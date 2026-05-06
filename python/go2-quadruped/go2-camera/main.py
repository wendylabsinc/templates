#!/usr/bin/env python3
"""go2-camera: WebRTC → MJPEG bridge for the Go2's front camera.

Connects to the dog's onboard WebRTC service, decodes H.264 frames into
JPEGs, and re-serves them as a multipart MJPEG stream on HTTP. Drop-in
substitute for the `realsense` HTTP server when the consumer (go2-RC)
just wants `${URL}/stream/color`.

Why a separate project from go2-Watchtower:
- watchtower is a 3.3 GB ROS2/Cyclone/Foxglove image; this is a pure
  FastAPI shim (~300 MB), so wendy run is fast enough to iterate on.
- go2-RC reads MJPEG over HTTP, not DDS, so the watchtower output
  format wouldn't help it anyway.

Endpoints:
  GET /health         → {"status": ..., "frames": <count>, "fps": ...}
  GET /stream/color   → multipart/x-mixed-replace MJPEG, latest-frame-wins

WebRTC quirks (same scar tissue as watchtower's go2_video_bridge):
- Only one WebRTC client per Go2 main controller. If the Unitree phone
  app is open, this can't connect.
- aiortc's H.264 decoder can stay wedged on a partial GOP; we send PLI
  (RTCP Picture Loss Indication) every few seconds until the first
  frame decodes.
- track.recv() raises MediaStreamError when the dog drops the track
  (motion-mode switch, phone app stealing the slot, etc). After 5
  consecutive errors we os._exit(1) so wendy's restart-on-failure
  brings us back with a fresh handshake.
"""

import asyncio
import dataclasses
import json
import logging
import os
import queue
from typing import Optional
import threading
import time

import cv2
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from unitree_webrtc_connect import (
    UnitreeWebRTCConnection,
    WebRTCConnectionMethod,
)

from audio import AudioOutPublisher
from perception import LidarSubscriber, PerceptionState
from webrtc_audio import OutboundPCMTrack, attach_outbound_audio

GO2_IP = os.environ.get("GO2_IP", "192.168.123.161")
PORT = int(os.environ.get("PORT", "8000"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "80"))
KEYFRAME_REQUEST_INTERVAL_S = 3.0
RECONNECT_BACKOFF_S = 2.0
MAX_CONSECUTIVE_TRACK_ERRORS = 5

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("go2-camera")
# aiortc spams "H264Decoder() failed to decode" until the first I-frame arrives.
logging.getLogger("aiortc.codecs.h264").setLevel(logging.ERROR)

# unitree_webrtc_connect logs "Receiving audio frame" / "Receiving video
# frame" on the root logger at INFO for every single frame — that's
# 50/sec for audio + 30/sec for video, which buries everything else
# under thousands of lines per minute. Drop just those two messages
# without silencing the rest of root's INFO output (we still want to
# see heartbeats, network status, validation, etc).
class _DropFrameSpam(logging.Filter):
    SPAM = ("Receiving audio frame", "Receiving video frame")
    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage() not in self.SPAM
logging.getLogger().addFilter(_DropFrameSpam())


class CameraState:
    """Shared state between the WebRTC worker thread and the FastAPI server.

    `frames` is a 1-slot queue: we always drop the previous frame on
    arrival so MJPEG consumers see the freshest possible image
    (latest-frame-wins). Several clients can read from `frames` only
    one at a time; for now we assume a single consumer (go2-RC). If we
    ever fan out to multiple browsers, switch to a per-client
    asyncio.Event + a single shared `latest` slot.
    """

    def __init__(self) -> None:
        self.frames: "queue.Queue" = queue.Queue(maxsize=1)
        self.first_frame_logged = False
        self.frame_count = 0
        self.last_frame_t = 0.0
        self.fps = 0.0
        self._fps_window_t = time.monotonic()
        self._fps_window_count = 0

    def push(self, img) -> None:
        try:
            self.frames.get_nowait()
        except queue.Empty:
            pass
        self.frames.put_nowait(img)
        self.frame_count += 1
        now = time.monotonic()
        self.last_frame_t = now
        self._fps_window_count += 1
        elapsed = now - self._fps_window_t
        if elapsed >= 1.0:
            self.fps = self._fps_window_count / elapsed
            self._fps_window_count = 0
            self._fps_window_t = now


state = CameraState()
perception_state = PerceptionState()
lidar_sub = LidarSubscriber(perception_state)
audio_out = AudioOutPublisher()
# Set when WebRTC connects + audiohub enters megaphone mode. Used by
# the /api/test_beep diagnostic to verify upload/play works without
# fighting the WebRTC slot held by the running connection.
_audiohub_ref = None
# Reference to the live UnitreeWebRTCConnection — we need it to call
# `conn.audio.switchAudioChannel(True)` when re-arming the megaphone
# after an idle timeout (the audiohub drops out of megaphone after
# some quiet seconds; we re-enter on demand from /ws/talk and
# /api/test_tone).
_conn_ref = None
# Time of last successful enter_megaphone(). After ~30 s of silence
# the dog drops back out; we re-enter if it's been longer than this
# window since the last refresh.
_megaphone_last_refresh = 0.0
_megaphone_refresh_lock = asyncio.Lock()
# The asyncio event loop running inside the WebRTC worker thread.
# `enter_megaphone()` and `switchAudioChannel()` send data over the
# aiortc data channel which is bound to that loop — calling them
# from FastAPI's loop leads to "got Future attached to a different
# loop" failures or silent no-ops. We bounce control calls onto the
# right loop via `asyncio.run_coroutine_threadsafe`.
_webrtc_loop: "Optional[asyncio.AbstractEventLoop]" = None


async def ensure_megaphone(min_age_s: float = 5.0) -> bool:
    """Re-enter audiohub megaphone mode if it's been idle.

    Returns True if megaphone is (now) active, False if we don't have
    a hub reference yet. Cheap to call repeatedly: only actually fires
    `enter_megaphone()` when the last refresh is older than
    `min_age_s`. Lock-serialised so concurrent talk WS clients don't
    stampede the audiohub with parallel api_id=4001 requests.
    """
    global _megaphone_last_refresh
    if _audiohub_ref is None:
        return False
    now = time.monotonic()
    if now - _megaphone_last_refresh < min_age_s:
        return True
    async with _megaphone_refresh_lock:
        # Re-check inside the lock — another caller may have just
        # refreshed while we were waiting.
        now = time.monotonic()
        if now - _megaphone_last_refresh < min_age_s:
            return True
        if _webrtc_loop is None:
            log.warning("ensure_megaphone: webrtc loop not set yet")
            return False
        try:
            # Bounce onto the WebRTC thread's loop because the data
            # channel send inside enter_megaphone() is bound to it.
            fut = asyncio.run_coroutine_threadsafe(
                _audiohub_ref.enter_megaphone(), _webrtc_loop,
            )
            await asyncio.wrap_future(fut)
            if _conn_ref is not None:
                try:
                    _conn_ref.audio.switchAudioChannel(True)
                except Exception:
                    log.exception("ensure_megaphone: switchAudioChannel failed")
            _megaphone_last_refresh = now
            log.info("ensure_megaphone: re-entered megaphone mode")
            return True
        except Exception:
            log.exception("ensure_megaphone: enter_megaphone failed")
            return False
# Outbound audio track injected into the same aiortc PeerConnection
# that's already negotiated for video. Browser PCM → push_pcm() →
# RTP → dog speaker. Confirmed live path on this firmware (DDS
# `/audioreceiver` does not work in this lib version).
outbound_audio = OutboundPCMTrack()
# Reference to the active aiortc PeerConnection — set after connect.
# The /api/webrtc_info diagnostic introspects it for codec/SDP info.
_pc_ref = None

# WebSocket clients streaming perception JSON. Each client is a
# (websocket, asyncio.Queue) pair — we push snapshots into every
# client's queue from a single ticker, and each WS handler drains
# its own queue. This keeps fan-out cheap and back-pressure local
# (a slow client doesn't block the others).
_ws_clients: "list[tuple[WebSocket, asyncio.Queue]]" = []
_ws_clients_lock = threading.Lock()


# -------------------------- WebRTC worker --------------------------


async def _on_track(track, state: CameraState) -> None:
    consecutive_errors = 0
    while True:
        try:
            frame = await track.recv()
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors == 1:
                log.warning(
                    "track.recv() error (%s); track likely dead — "
                    "phone Go2 app open?", type(e).__name__,
                )
            if consecutive_errors >= MAX_CONSECUTIVE_TRACK_ERRORS:
                log.error(
                    "track.recv() failed %d times in a row; exiting so "
                    "wendy restarts us with a fresh WebRTC handshake.",
                    consecutive_errors,
                )
                os._exit(1)
            await asyncio.sleep(0.1)
            continue
        img = frame.to_ndarray(format="bgr24")
        if not state.first_frame_logged:
            state.first_frame_logged = True
            log.info("First video frame decoded: %dx%d", img.shape[1], img.shape[0])
        state.push(img)


async def _keyframe_nag(conn, state: CameraState) -> None:
    while not state.first_frame_logged:
        await asyncio.sleep(KEYFRAME_REQUEST_INTERVAL_S)
        if state.first_frame_logged or conn is None:
            return
        try:
            for transceiver in conn.pc.getTransceivers():
                receiver = transceiver.receiver
                track = getattr(receiver, "track", None)
                if track is None or track.kind != "video":
                    continue
                send_pli = getattr(receiver, "_send_rtcp_pli", None)
                ssrc = getattr(receiver, "_ssrc", None) or getattr(
                    receiver, "_track_id", None
                )
                if send_pli and ssrc is not None:
                    asyncio.ensure_future(send_pli(ssrc))
                    log.info("Requested H.264 keyframe (PLI)")
        except Exception as e:
            log.warning("Keyframe request failed: %s", e)


async def _webrtc_main() -> None:
    global _pc_ref, _conn_ref, _webrtc_loop
    _webrtc_loop = asyncio.get_running_loop()
    log.info("Connecting to Go2 at %s …", GO2_IP)
    conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=GO2_IP)
    await conn.connect()
    _pc_ref = conn.pc  # for /api/webrtc_info diagnostic
    _conn_ref = conn  # for ensure_megaphone() to call switchAudioChannel
    conn.video.switchVideoChannel(True)
    conn.video.add_track_callback(lambda t: _on_track(t, state))
    asyncio.create_task(_keyframe_nag(conn, state))

    # Open the dog's audiohub in megaphone (live-audio) mode. Without
    # this, audio bytes published to rt/audioreceiver are received by
    # the audiohub service but not played out the speaker — that was
    # the source of all the "noise" we heard during earlier debugging.
    # `enter_megaphone()` is a coroutine on this lib version (the
    # inspect dump showed it as a plain function but the runtime
    # RuntimeWarning revealed it's actually async). We await it inside
    # this already-async function. Log + swallow failures so a missing
    # audio path doesn't kill the video bridge.
    global _audiohub_ref
    try:
        from unitree_webrtc_connect.webrtc_audiohub import WebRTCAudioHub
        hub = WebRTCAudioHub(conn)
        await hub.enter_megaphone()
        global _megaphone_last_refresh
        _megaphone_last_refresh = time.monotonic()
        log.info("audiohub: entered megaphone mode")
        try:
            conn.audio.switchAudioChannel(True)
            log.info("audiohub: audio channel switched ON (speaker live)")
        except Exception:
            log.exception("audiohub: switchAudioChannel failed (continuing)")
        # Stash for the /api/test_beep diagnostic endpoint to use.
        _audiohub_ref = hub

        # Hook our outbound PCM track into the existing PeerConnection.
        # Tries replaceTrack() on the existing audio transceiver first
        # (no renegotiation), falls back to addTrack() if needed.
        try:
            mode = await attach_outbound_audio(conn.pc, outbound_audio)
            log.info("audiohub: outbound audio track attached (%s)", mode)
        except Exception:
            log.exception("audiohub: outbound audio track attach failed")
    except Exception:
        log.exception("audiohub: enter_megaphone failed; speaker may stay silent")

    while True:
        await asyncio.sleep(1)


def _run_webrtc_thread() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        try:
            loop.run_until_complete(_webrtc_main())
        except Exception as e:
            log.error("WebRTC connection failed: %s; retrying in %.1fs",
                      e, RECONNECT_BACKOFF_S)
            time.sleep(RECONNECT_BACKOFF_S)


# -------------------------- HTTP server --------------------------


app = FastAPI(title="go2-camera", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    threading.Thread(target=_run_webrtc_thread, daemon=True).start()
    lidar_sub.start()
    asyncio.create_task(_perception_broadcaster())


async def _perception_broadcaster() -> None:
    """Push the latest perception snapshot to every connected WS client at 10 Hz.

    We sample on a fixed cadence rather than reacting to every lidar
    sample because the lidar runs at 10 Hz already; oversampling wastes
    work, undersampling loses freshness. Snapshot dataclass → dict via
    `dataclasses.asdict` → JSON once, reused for all clients.
    """
    period_s = 1.0 / 10.0
    last_stamp = 0
    while True:
        await asyncio.sleep(period_s)
        snap = perception_state.latest()
        if not snap.have_data or snap.stamp_ns == last_stamp:
            continue
        last_stamp = snap.stamp_ns
        payload = json.dumps(
            dataclasses.asdict(snap), separators=(",", ":")
        )
        with _ws_clients_lock:
            clients = list(_ws_clients)
        for _, q in clients:
            # Drop the oldest if a client is slow — perception is
            # latest-frame-wins, no value in queuing stale frames.
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    pass


@app.get("/health")
async def health() -> JSONResponse:
    healthy = state.first_frame_logged and (
        time.monotonic() - state.last_frame_t < 5.0
    )
    return JSONResponse({
        "status": "ok" if healthy else "starting",
        "frames": state.frame_count,
        "fps": round(state.fps, 1),
        "go2_ip": GO2_IP,
    })


def _mjpeg_generator():
    """Yields multipart MJPEG chunks; sleeps when there's no new frame.

    We block-with-timeout on the queue so we don't spin if frames stop
    flowing; consumers will simply see the stream stall, which is the
    correct behaviour (HTTP keep-alive will eventually drop them).
    """
    boundary = b"--frame"
    while True:
        try:
            img = state.frames.get(timeout=2.0)
        except queue.Empty:
            continue
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            continue
        jpg = buf.tobytes()
        yield (
            boundary
            + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(jpg)).encode()
            + b"\r\n\r\n"
            + jpg
            + b"\r\n"
        )


def _synth_bark_pcm(rate: int = 48000) -> bytes:
    """Build a 2-woof bark as Int16 mono PCM at `rate`.

    Each woof is a downward chirp (~700 Hz → 250 Hz over 130 ms) plus
    a band-limited noise layer, shaped by an attack-decay envelope.
    Two woofs separated by ~120 ms of silence — recognisable enough
    over a small speaker without needing a real recording on disk.
    """
    import math
    import random
    import struct

    def woof(dur_s: float) -> "list[int]":
        n = int(rate * dur_s)
        out = [0] * n
        f0, f1 = 700.0, 250.0
        phase = 0.0
        for i in range(n):
            t = i / rate
            # Attack 8 ms, exponential decay over the rest.
            atk = min(1.0, t / 0.008)
            dec = math.exp(-(t / (dur_s * 0.45)))
            env = atk * dec
            # Linear chirp f0 → f1 over duration.
            f = f0 + (f1 - f0) * (t / dur_s)
            phase += 2.0 * math.pi * f / rate
            tone = math.sin(phase)
            noise = (random.random() * 2.0 - 1.0) * 0.35
            s = (tone * 0.65 + noise) * env
            out[i] = max(-32767, min(32767, int(s * 0.55 * 32767)))
        return out

    silence_n = int(rate * 0.12)
    samples = woof(0.13) + [0] * silence_n + woof(0.16)
    return b"".join(struct.pack("<h", s) for s in samples)


@app.post("/api/bark")
async def bark() -> JSONResponse:
    """Play a synthesized two-woof bark out the dog's speaker."""
    armed = await ensure_megaphone(min_age_s=0.0)
    if not armed:
        return JSONResponse(
            {"ok": False, "reason": "megaphone unavailable"}, status_code=503,
        )

    pcm = _synth_bark_pcm(rate=48000)
    frame_bytes = 960 * 2  # 20 ms of 48 kHz mono Int16
    pushed = 0
    for off in range(0, len(pcm), frame_bytes):
        chunk = pcm[off : off + frame_bytes]
        if len(chunk) == frame_bytes:
            outbound_audio.push_pcm(chunk)
            pushed += 1
            await asyncio.sleep(0.02)
    return JSONResponse({"ok": True, "pushed_frames": pushed})


@app.post("/api/test_tone")
async def test_tone() -> JSONResponse:
    """Push a 1.5-second 440 Hz sine wave directly into the outbound
    WebRTC audio track. Bypasses the browser entirely.

    If the dog plays a clean steady tone → the WebRTC injection +
    Opus encoding + dog-side decode are all fine, and the chipmunk
    we hear during PTT is purely a browser-side capture-rate issue.

    If the dog plays a chipmunky / wrong-pitch tone → the bug is in
    our AudioFrame construction (sample_rate, layout, pts pacing).
    """
    import math
    import struct

    # Re-arm megaphone so an idle-timed-out session doesn't silently
    # eat the tone (we observed this — pushed_frames=75 returned ok
    # but no audio came out the dog after ~minutes of inactivity).
    armed = await ensure_megaphone(min_age_s=0.0)
    if not armed:
        return JSONResponse(
            {"ok": False, "reason": "megaphone unavailable"}, status_code=503,
        )

    rate = 48000
    duration_s = 1.5
    freq = 440.0
    n = int(rate * duration_s)
    amp = int(0.4 * 32767)

    # Build the whole sine wave as one Int16 mono PCM blob, then push
    # it into the track in 20 ms (960-sample) chunks at the correct
    # cadence. This matches what the browser is supposed to do.
    samples = (
        struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(n)
    )
    pcm = b"".join(samples)
    frame_bytes = 960 * 2  # 960 mono samples at 48 kHz = 20 ms
    pushed = 0
    for off in range(0, len(pcm), frame_bytes):
        chunk = pcm[off : off + frame_bytes]
        if len(chunk) == frame_bytes:
            outbound_audio.push_pcm(chunk)
            pushed += 1
            await asyncio.sleep(0.02)  # pace at exactly 50 Hz
    return JSONResponse({"ok": True, "pushed_frames": pushed,
                         "rate": rate, "freq": freq, "duration_s": duration_s})


@app.get("/api/webrtc_info")
async def webrtc_info() -> JSONResponse:
    """Inspect the running WebRTC PeerConnection: SDP + transceiver
    directions + per-transceiver codec names. Useful for diagnosing
    why outbound audio sounds wrong (codec/sample-rate mismatch).
    """
    if _pc_ref is None:
        return JSONResponse({"ok": False, "reason": "pc not set yet"}, status_code=503)
    pc = _pc_ref
    transceivers = []
    for tr in pc.getTransceivers():
        sender_codecs = []
        receiver_codecs = []
        try:
            for c in (tr.sender.getCapabilities("audio").codecs
                      if tr.sender else []):
                sender_codecs.append(getattr(c, "mimeType", repr(c)))
        except Exception:
            pass
        try:
            params = tr.sender.getParameters() if tr.sender else None
            if params and getattr(params, "codecs", None):
                sender_codecs = [getattr(c, "mimeType", repr(c))
                                 for c in params.codecs]
        except Exception:
            pass
        kind = "?"
        try:
            kind = (tr.receiver.track.kind
                    if tr.receiver and tr.receiver.track else
                    tr.sender.track.kind if tr.sender and tr.sender.track else "?")
        except Exception:
            pass
        transceivers.append({
            "kind": kind,
            "direction": getattr(tr, "direction", "?"),
            "currentDirection": getattr(tr, "currentDirection", "?"),
            "sender_codecs": sender_codecs,
        })
    return JSONResponse({
        "ok": True,
        "local_sdp": pc.localDescription.sdp if pc.localDescription else None,
        "remote_sdp": pc.remoteDescription.sdp if pc.remoteDescription else None,
        "transceivers": transceivers,
    })


@app.post("/api/test_beep")
async def test_beep() -> JSONResponse:
    """Diagnostic: synthesize a 1.5s sine wave WAV, upload via the
    audiohub, and ask the dog to play it. If you hear a beep on the
    dog, file-based megaphone playback works — the missing piece for
    live mic→speaker is just the streaming path (DDS topic vs WebRTC).
    """
    import asyncio as _asyncio
    import math
    import struct
    import tempfile
    import wave

    if _audiohub_ref is None:
        return JSONResponse(
            {"ok": False, "reason": "audiohub not initialised yet"},
            status_code=503,
        )

    # 1.5 s sine at 880 Hz, 16-bit PCM mono, 16 kHz.
    rate, dur, freq, amp = 16000, 1.5, 880.0, int(0.4 * 32767)
    n = int(dur * rate)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(n)
        ))

    log.info("test_beep: uploading %s (%d bytes)…", wav_path,
             __import__("os").path.getsize(wav_path))
    try:
        upload = _audiohub_ref.upload_megaphone(wav_path)
        if _asyncio.iscoroutine(upload):
            upload = await upload
        log.info("test_beep: upload_megaphone returned: %r", upload)

        played = None
        if isinstance(upload, str) and upload:
            played = _audiohub_ref.play_by_uuid(upload)
            if _asyncio.iscoroutine(played):
                played = await played
            log.info("test_beep: play_by_uuid returned: %r", played)

        return JSONResponse({
            "ok": True,
            "upload": str(upload),
            "played": str(played) if played is not None else None,
        })
    except Exception as exc:
        log.exception("test_beep failed")
        return JSONResponse(
            {"ok": False, "reason": str(exc)},
            status_code=500,
        )


@app.get("/stream/color")
def stream_color() -> StreamingResponse:
    return StreamingResponse(
        _mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.websocket("/ws/talk")
async def ws_talk(ws: WebSocket) -> None:
    """Browser → dog speaker. Receives binary 8 kHz mono Int16 PCM frames.

    Each binary message is one audio chunk (typically 20 ms = 160 samples
    = 320 bytes). We don't validate length — the AudioData publisher
    handles whatever the browser sends, encoded as µ-law per chunk.
    Text frames are ignored (kept open for future control messages
    like start/stop markers if we ever need them).
    """
    await ws.accept()
    # Re-arm megaphone if it's been idle. Cheap if recently active
    # (5 s grace window). Force-refresh by passing min_age_s=0.0 if
    # the dog has been silent for minutes.
    await ensure_megaphone(min_age_s=0.0)
    log.info("talk WS: client connected")
    n_frames = 0
    n_bytes = 0
    first_frame_logged = False
    import time as _time
    t_start = _time.monotonic()
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data:
                if not first_frame_logged:
                    first_frame_logged = True
                    log.info(
                        "talk WS: first frame received, len=%d bytes (= %d "
                        "Int16 mono samples = %.1f ms at 48kHz)",
                        len(data), len(data) // 2,
                        1000.0 * (len(data) // 2) / 48000.0,
                    )
                n_frames += 1
                n_bytes += len(data)
                outbound_audio.push_pcm(data)
    except WebSocketDisconnect:
        pass
    finally:
        dt = _time.monotonic() - t_start
        log.info(
            "talk WS: client disconnected after %.2fs — %d frames, %d bytes "
            "(%.1f frames/s, %.1f KB/s)",
            dt, n_frames, n_bytes,
            n_frames / dt if dt > 0 else 0,
            (n_bytes / 1024) / dt if dt > 0 else 0,
        )


@app.websocket("/ws/perception")
async def ws_perception(ws: WebSocket) -> None:
    """Stream perception snapshots (free_space + scan_xy) at 10 Hz.

    Browser opens this WS, draws the lidar canvas + tints the vignette
    based on `free_space_min_m`. On disconnect we reap the client from
    the broadcast list.
    """
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    with _ws_clients_lock:
        _ws_clients.append((ws, q))
    try:
        while True:
            payload = await q.get()
            await ws.send_text(payload)
    except (WebSocketDisconnect, RuntimeError):
        # RuntimeError covers "Cannot call send once a close message
        # has been sent" if the client closes between send + queue get.
        pass
    finally:
        with _ws_clients_lock:
            try:
                _ws_clients.remove((ws, q))
            except ValueError:
                pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
