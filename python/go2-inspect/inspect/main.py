#!/usr/bin/env python3
"""go2-inspect: open-vocabulary object inspection over the Go2's front camera.

Single service. A background thread holds a WebRTC connection to the dog and decodes H.264
into BGR frames (latest-frame-wins). A second background thread lazily loads a YOLOE model.
The FastAPI app serves:

  GET  /health              → liveness + camera/model readiness
  GET  /stream/raw          → MJPEG of the latest camera frame (cheap, always available)
  GET  /stream/annotated    → MJPEG with YOLOE boxes, inference capped at YOLO_MAX_FPS
  GET  /api/prompts         → the current open-vocab detection prompts
  POST /api/prompts         → set them (recomputes text embeddings) {"prompts": [...]}
  POST /api/capture         → detect on the latest frame; save annotated jpg + crops + report
  GET  /api/report          → the running report as JSON + capture image URLs
  GET  /captures/<path>     → serve a saved capture / crop image
  GET  /                    → the web UI

The camera worker is the video-only half of go2-rc's camera service — all the audio /
megaphone / lidar / perception paths are dropped, since inspection only needs the video track.

WebRTC quirks (same scar tissue as go2-rc's camera bridge):
- Only one WebRTC client per Go2 main controller. If the Unitree phone app is open, this
  can't connect.
- aiortc's H.264 decoder can stay wedged on a partial GOP; we send PLI (RTCP Picture Loss
  Indication) every few seconds until the first frame decodes.
- track.recv() raises MediaStreamError when the dog drops the track. After 5 consecutive
  errors we os._exit(1) so wendy's restart-on-failure brings us back with a fresh handshake.
"""

import asyncio
import logging
import os
import queue
import threading
import time

import cv2
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod

import report
from detector import DEFAULT_PROMPTS, Detector

GO2_IP = os.environ.get("GO2_IP", "192.168.123.161")
PORT = int(os.environ.get("PORT", "3400"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "80"))
YOLO_MAX_FPS = float(os.environ.get("YOLO_MAX_FPS", "2"))
CAPTURE_DIR_ENV = os.environ.get("CAPTURE_DIR", "/data/captures")
KEYFRAME_REQUEST_INTERVAL_S = 3.0
RECONNECT_BACKOFF_S = 2.0
MAX_CONSECUTIVE_TRACK_ERRORS = 5

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("go2-inspect")
logging.getLogger("aiortc.codecs.h264").setLevel(logging.ERROR)


# unitree_webrtc_connect logs "Receiving video frame" on the root logger at INFO for every
# frame (30/sec), burying everything else. Drop just that message.
class _DropFrameSpam(logging.Filter):
    SPAM = ("Receiving audio frame", "Receiving video frame")

    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage() not in self.SPAM


logging.getLogger().addFilter(_DropFrameSpam())


def _resolve_capture_dir() -> str:
    """Prefer CAPTURE_DIR (usually a /data volume); fall back to /app/captures if unwritable."""
    for candidate in (CAPTURE_DIR_ENV, "/app/captures"):
        try:
            os.makedirs(candidate, exist_ok=True)
            testfile = os.path.join(candidate, ".writetest")
            with open(testfile, "w") as f:
                f.write("ok")
            os.remove(testfile)
            return candidate
        except Exception:
            log.warning("capture dir %s not writable, trying next", candidate)
    # Last resort: cwd-relative (still lets the service run for stream-only use).
    os.makedirs("captures", exist_ok=True)
    return os.path.abspath("captures")


CAPTURE_DIR = _resolve_capture_dir()


class CameraState:
    """Shared state between the WebRTC worker thread and the FastAPI server.

    `frames` is a 1-slot queue (latest-frame-wins) feeding MJPEG streams. `latest` holds the
    most recent frame for one-shot readers (capture / annotated inference) that must not
    steal from the stream queue.
    """

    def __init__(self) -> None:
        self.frames: "queue.Queue" = queue.Queue(maxsize=1)
        self.latest = None
        self.first_frame_logged = False
        self.frame_count = 0
        self.last_frame_t = 0.0
        self.fps = 0.0
        self._fps_window_t = time.monotonic()
        self._fps_window_count = 0

    def push(self, img) -> None:
        self.latest = img
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

# The detector is loaded lazily in a background thread so the service comes up (and the raw
# stream works) immediately, even while the YOLOE weights are still loading. `detector_error`
# records why the model failed to load, surfaced via /health.
detector: "Detector | None" = None
detector_error: "str | None" = None
_detector_lock = threading.Lock()


def _load_detector() -> None:
    global detector, detector_error
    try:
        log.info("Loading YOLOE detector …")
        d = Detector(prompts=DEFAULT_PROMPTS)
        with _detector_lock:
            detector = d
        log.info("YOLOE detector ready (%d prompts)", len(d.prompts))
    except Exception as exc:  # noqa: BLE001 — degrade gracefully, stream still works
        detector_error = f"{type(exc).__name__}: {exc}"
        log.error("YOLOE detector failed to load: %s", detector_error)


# -------------------------- WebRTC worker (video only) --------------------------


async def _on_track(track, st: CameraState) -> None:
    consecutive_errors = 0
    while True:
        try:
            frame = await track.recv()
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors == 1:
                log.warning(
                    "track.recv() error (%s); track likely dead — phone Go2 app open?",
                    type(e).__name__,
                )
            if consecutive_errors >= MAX_CONSECUTIVE_TRACK_ERRORS:
                log.error(
                    "track.recv() failed %d times; exiting so wendy restarts us with a "
                    "fresh WebRTC handshake.",
                    consecutive_errors,
                )
                os._exit(1)
            await asyncio.sleep(0.1)
            continue
        img = frame.to_ndarray(format="bgr24")
        if not st.first_frame_logged:
            st.first_frame_logged = True
            log.info("First video frame decoded: %dx%d", img.shape[1], img.shape[0])
        st.push(img)


async def _keyframe_nag(conn, st: CameraState) -> None:
    while not st.first_frame_logged:
        await asyncio.sleep(KEYFRAME_REQUEST_INTERVAL_S)
        if st.first_frame_logged or conn is None:
            return
        try:
            for transceiver in conn.pc.getTransceivers():
                receiver = transceiver.receiver
                track = getattr(receiver, "track", None)
                if track is None or track.kind != "video":
                    continue
                send_pli = getattr(receiver, "_send_rtcp_pli", None)
                ssrc = getattr(receiver, "_ssrc", None) or getattr(receiver, "_track_id", None)
                if send_pli and ssrc is not None:
                    asyncio.ensure_future(send_pli(ssrc))
                    log.info("Requested H.264 keyframe (PLI)")
        except Exception as e:
            log.warning("Keyframe request failed: %s", e)


async def _webrtc_main() -> None:
    log.info("Connecting to Go2 at %s …", GO2_IP)
    conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=GO2_IP)
    await conn.connect()
    conn.video.switchVideoChannel(True)
    conn.video.add_track_callback(lambda t: _on_track(t, state))
    asyncio.create_task(_keyframe_nag(conn, state))
    while True:
        await asyncio.sleep(1)


def _run_webrtc_thread() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        try:
            loop.run_until_complete(_webrtc_main())
        except Exception as e:
            log.error(
                "WebRTC connection failed: %s; retrying in %.1fs", e, RECONNECT_BACKOFF_S
            )
            time.sleep(RECONNECT_BACKOFF_S)


# -------------------------- HTTP server --------------------------


app = FastAPI(title="go2-inspect", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    threading.Thread(target=_run_webrtc_thread, daemon=True).start()
    threading.Thread(target=_load_detector, daemon=True).start()


class PromptsBody(BaseModel):
    prompts: list[str]


def _encode_jpeg(img) -> "bytes | None":
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return buf.tobytes() if ok else None


def _mjpeg_chunk(jpg: bytes) -> bytes:
    return (
        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
        + str(len(jpg)).encode()
        + b"\r\n\r\n"
        + jpg
        + b"\r\n"
    )


@app.get("/health")
async def health() -> JSONResponse:
    cam_ok = state.first_frame_logged and (time.monotonic() - state.last_frame_t < 5.0)
    with _detector_lock:
        model_ready = detector is not None
    return JSONResponse(
        {
            "status": "ok" if cam_ok else "starting",
            "camera": {"ok": cam_ok, "frames": state.frame_count, "fps": round(state.fps, 1)},
            "detector": {
                "ready": model_ready,
                "error": detector_error,
                "prompts": detector.prompts if model_ready else DEFAULT_PROMPTS,
            },
            "go2_ip": GO2_IP,
        }
    )


def _raw_generator():
    """Yield MJPEG chunks from the shared 1-slot queue; block-with-timeout to avoid spinning."""
    while True:
        try:
            img = state.frames.get(timeout=2.0)
        except queue.Empty:
            continue
        jpg = _encode_jpeg(img)
        if jpg:
            yield _mjpeg_chunk(jpg)


def _annotated_generator():
    """Yield MJPEG chunks with YOLOE boxes, throttled to YOLO_MAX_FPS.

    Inference is best-effort: on CPU it can't keep up with the camera, so we cap it and reuse
    the last annotated frame between inferences. If the model isn't loaded yet we fall back to
    the raw frame so the stream never blanks."""
    min_period = 1.0 / YOLO_MAX_FPS if YOLO_MAX_FPS > 0 else 0.0
    last_infer_t = 0.0
    while True:
        img = state.latest
        if img is None:
            time.sleep(0.1)
            continue
        with _detector_lock:
            det = detector
        annotated = img
        now = time.monotonic()
        if det is not None and (now - last_infer_t) >= min_period:
            try:
                detections = det.detect(img)
                annotated = report.draw_detections(img, detections)
                last_infer_t = now
            except Exception as e:  # noqa: BLE001 — keep streaming raw on inference error
                log.warning("annotated inference failed: %s", e)
        jpg = _encode_jpeg(annotated)
        if jpg:
            yield _mjpeg_chunk(jpg)
        # Pace the loop so a fast camera + slow CPU don't busy-spin encoding.
        time.sleep(max(0.0, min_period * 0.5))


@app.get("/stream/raw")
def stream_raw() -> StreamingResponse:
    return StreamingResponse(
        _raw_generator(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/stream/annotated")
def stream_annotated() -> StreamingResponse:
    return StreamingResponse(
        _annotated_generator(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/api/prompts")
async def get_prompts() -> dict:
    with _detector_lock:
        d = detector
    return {"prompts": d.prompts if d else DEFAULT_PROMPTS, "ready": d is not None}


@app.post("/api/prompts")
async def set_prompts(body: PromptsBody) -> dict:
    with _detector_lock:
        d = detector
    if d is None:
        raise HTTPException(503, detector_error or "detector still loading")
    try:
        names = d.set_prompts(body.prompts)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    log.info("prompts set to %s", names)
    return {"prompts": names}


@app.post("/api/capture")
async def capture() -> dict:
    """Detect on the latest frame; save an annotated jpg + per-object crops; append to report."""
    img = state.latest
    if img is None:
        raise HTTPException(503, "no camera frame yet")
    with _detector_lock:
        d = detector
    if d is None:
        raise HTTPException(503, detector_error or "detector still loading")
    # Offload the (blocking, CPU-heavy) inference + disk writes to a worker thread so we don't
    # stall the event loop.
    detections = await asyncio.to_thread(d.detect, img)
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    result = await asyncio.to_thread(
        report.save_capture, CAPTURE_DIR, ts, img, detections, JPEG_QUALITY
    )
    log.info("capture %s: %d objects", ts, len(result["objects"]))
    return result


@app.get("/api/report")
async def get_report() -> dict:
    captures = await asyncio.to_thread(report.load_report, CAPTURE_DIR)
    # Rewrite relative image names to servable /captures/ URLs.
    for c in captures:
        c["annotated_url"] = f"/captures/{c['annotated']}"
        for o in c["objects"]:
            o["crop_url"] = f"/captures/{o['crop']}" if o.get("crop") else None
    return {"captures": captures, "capture_dir": CAPTURE_DIR}


@app.get("/captures/{name}")
async def serve_capture(name: str) -> FileResponse:
    # Guard against path traversal — only serve plain filenames from the capture dir.
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "bad name")
    path = os.path.join(CAPTURE_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return FileResponse(path)


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    target = os.path.join(STATIC_DIR, "index.html")
    if not os.path.isfile(target):
        raise HTTPException(500, f"index.html missing at {target}")
    return FileResponse(target, media_type="text/html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info", access_log=False)
