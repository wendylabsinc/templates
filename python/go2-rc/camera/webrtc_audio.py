"""Outbound audio track for the existing aiortc PeerConnection.

The DDS topic `/audioreceiver` turned out NOT to be the live-streaming
path on this Go2 firmware — uploading a WAV via the audiohub does play
through the speaker (verified by the /api/test_beep diagnostic), but
publishing PCM/µ-law/A-law to the topic produces nothing audible.

The dog's phone app does live two-way intercom over the WebRTC audio
track itself. So we mirror that: add an outbound audio track to the
same `pc` go2-camera already holds for video, push browser-mic PCM
into a `MediaStreamTrack`'s queue, and let aiortc handle RTP framing
end-to-end.

To avoid the cost of a full SDP renegotiation (the dog's WebRTC stack
may or may not support it cleanly), we try `replaceTrack()` on the
existing audio transceiver's sender first — that swaps the outgoing
content without changing the SDP. If the transceiver is recvonly we
fall back to `addTrack()` which DOES trigger renegotiation; the
caller then has to fire whatever signaling path the lib uses.
"""

import asyncio
import collections
import fractions
import logging
import threading
import time
from typing import Optional

import numpy as np
from aiortc import MediaStreamTrack
from av.audio.frame import AudioFrame


log = logging.getLogger("go2-camera.webrtc_audio")


SAMPLE_RATE = 48000
SAMPLES_PER_FRAME = 960  # 20 ms at 48 kHz — matches WebRTC Opus framing
# Stereo (the dog negotiates `opus/48000/2`, i.e. 2 channels). The
# browser captures mono; we duplicate L=R when building the frame.
# `FRAME_BYTES` is the input mono PCM size (2 bytes/sample × 960
# samples). The actual AudioFrame holds 2× that since we duplicate.
FRAME_BYTES = SAMPLES_PER_FRAME * 2  # mono Int16 input


class OutboundPCMTrack(MediaStreamTrack):
    """A MediaStreamTrack that emits 8 kHz mono Int16 PCM as AudioFrames.

    Audio is fed in via `push_pcm(bytes)` — typically from the WebSocket
    handler that's receiving 20 ms PCM chunks from the browser. When
    the queue is empty, `recv()` returns silence frames so the WebRTC
    clock keeps ticking and the connection stays healthy.
    """

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        # maxlen sized for ~4 s of 20 ms frames. Smaller values (we tried
        # 12) silently drop most frames when the producer briefly outruns
        # the WebRTC thread's recv() drain — e.g. a 1.5 s test tone pushes
        # 75 frames at 50 Hz; with maxlen=12 the first 63 get evicted
        # before recv() ever sees them and the dog plays silence.
        self._buf: "collections.deque[bytes]" = collections.deque(maxlen=200)
        self._buf_lock = threading.Lock()
        self._residual = b""
        self._pts = 0
        self._next_frame_t: Optional[float] = None
        self._silence = b"\x00" * FRAME_BYTES
        # Diagnostics
        self._frames_total = 0
        self._frames_with_data = 0
        self._pushes_total = 0
        self._pushes_bytes = 0
        self._pushes_drops = 0
        self._last_stats_t = time.monotonic()
        log.info(
            "OutboundPCMTrack init: rate=%d, samples/frame=%d (%.1f ms), "
            "FRAME_BYTES=%d (mono input), output=stereo s16",
            SAMPLE_RATE, SAMPLES_PER_FRAME,
            1000.0 * SAMPLES_PER_FRAME / SAMPLE_RATE, FRAME_BYTES,
        )

    def push_pcm(self, pcm: bytes) -> None:
        """Append browser PCM. Safe to call from any thread / event loop."""
        if not pcm:
            return
        with self._buf_lock:
            was_full = len(self._buf) >= self._buf.maxlen
            self._buf.append(pcm)
            self._pushes_total += 1
            self._pushes_bytes += len(pcm)
            if was_full:
                self._pushes_drops += 1

    async def recv(self) -> AudioFrame:
        # Pace ourselves to one frame per 20 ms so RTP timestamps
        # stay sensible regardless of how fast the queue fills.
        now = time.monotonic()
        if self._next_frame_t is None:
            self._next_frame_t = now
        else:
            self._next_frame_t += SAMPLES_PER_FRAME / SAMPLE_RATE
            wait = self._next_frame_t - now
            if wait > 0:
                await asyncio.sleep(wait)
            else:
                # Producer is behind real time — reset the clock so we
                # don't accumulate drift after a stall.
                self._next_frame_t = now

        pcm = self._residual
        self._residual = b""
        had_data_at_start = bool(pcm)
        while len(pcm) < FRAME_BYTES:
            with self._buf_lock:
                if not self._buf:
                    break
                pcm += self._buf.popleft()
                had_data_at_start = True
        if len(pcm) >= FRAME_BYTES:
            self._residual = pcm[FRAME_BYTES:]
            pcm = pcm[:FRAME_BYTES]
        else:
            pcm = pcm + self._silence[: FRAME_BYTES - len(pcm)]
            had_data_at_start = had_data_at_start and (
                pcm[: FRAME_BYTES - len(self._silence)] != b""
            )

        # Periodic stats so we can see exactly what's flowing.
        # ─ pushes/s   : how often the browser is sending us PCM
        # ─ bytes/s    : confirms expected ~96 KB/s at 48 kHz Int16 mono
        # ─ recv/s     : aiortc's pacing (should be ~50 to match Opus 20 ms)
        # ─ data/recv  : how often recv() returns real (vs silent) audio
        # ─ buf        : current deque depth at sample time
        self._frames_total += 1
        if had_data_at_start:
            self._frames_with_data += 1
        elapsed = now - self._last_stats_t
        if elapsed > 5.0:
            with self._buf_lock:
                buf_depth = len(self._buf)
            push_rate = self._pushes_total / elapsed
            byte_rate = self._pushes_bytes / elapsed
            recv_rate = self._frames_total / elapsed
            data_pct = (
                100.0 * self._frames_with_data / self._frames_total
                if self._frames_total else 0.0
            )
            log.info(
                "outbound audio @ %.1fs: pushes=%d (%.1f/s, %.1f KB/s, drops=%d) "
                "| recv=%d (%.1f/s, %.0f%% with data) | buf_depth=%d",
                elapsed, self._pushes_total, push_rate, byte_rate / 1024,
                self._pushes_drops, self._frames_total, recv_rate, data_pct,
                buf_depth,
            )
            self._frames_total = 0
            self._frames_with_data = 0
            self._pushes_total = 0
            self._pushes_bytes = 0
            self._pushes_drops = 0
            self._last_stats_t = now

        # Build a stereo s16 frame: duplicate the mono PCM to L=R.
        # The negotiated codec is `opus/48000/2`; sending mono there
        # makes the decoder play back 2x faster (very high-pitched
        # noise was the symptom).
        # Layout `s16` (packed) needs interleaved L,R,L,R,…
        # so we use np.repeat on the mono array.
        mono = np.frombuffer(pcm, dtype=np.int16)
        stereo = np.repeat(mono, 2).reshape(1, -1)
        frame = AudioFrame.from_ndarray(stereo, format="s16", layout="stereo")
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
        self._pts += SAMPLES_PER_FRAME
        return frame


async def attach_outbound_audio(pc, track: OutboundPCMTrack) -> str:
    """Hook `track` up as the outbound audio on the existing pc.

    Returns one of:
      "replaced"  — found an existing audio transceiver and swapped
                    its sender's track via replaceTrack(); no SDP
                    renegotiation required.
      "added"     — no usable transceiver existed; called pc.addTrack().
                    The caller may need to drive renegotiation if the
                    remote peer expects a fresh offer.
      "no-pc"     — pc was None.

    We try replaceTrack() first because the dog's WebRTC stack does
    a single SDP exchange at connect time and may not handle a mid-
    session offer cleanly.
    """
    if pc is None:
        return "no-pc"

    for tr in pc.getTransceivers():
        recv = getattr(tr, "receiver", None)
        recv_track = getattr(recv, "track", None)
        if recv_track is None or recv_track.kind != "audio":
            continue
        sender = getattr(tr, "sender", None)
        if sender is None:
            continue
        try:
            # aiortc's RTCRtpSender.replaceTrack is sync in some versions
            # (returns None) and async in others (returns a coroutine).
            # Handle both: call it, and only await if we got a coroutine.
            result = sender.replaceTrack(track)
            if asyncio.iscoroutine(result):
                await result
            log.info("attached outbound audio via replaceTrack on existing transceiver")
            # Nudge transceiver direction to sendrecv so RTP actually
            # flows out — recvonly transceivers will accept the track
            # but the SDP says we don't send, and frames are dropped.
            try:
                if hasattr(tr, "direction") and tr.direction != "sendrecv":
                    tr.direction = "sendrecv"
            except Exception:
                pass
            return "replaced"
        except Exception:
            log.exception("replaceTrack failed; falling through to addTrack")
            break

    # No usable existing transceiver — add a fresh one. This typically
    # triggers a 'negotiationneeded' event; whether the lib handles
    # that is firmware-dependent.
    try:
        pc.addTrack(track)
        log.info("attached outbound audio via addTrack (renegotiation may be needed)")
        return "added"
    except Exception:
        log.exception("addTrack failed")
        return "failed"
