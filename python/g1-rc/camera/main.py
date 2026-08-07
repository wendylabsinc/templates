#!/usr/bin/env python3
"""g1-camera: OpenCV → MJPEG bridge for the G1's front camera.

G1 doesn't expose a Unitree WebRTC service like Go2; cameras are
read via OpenCV's VideoCapture against whatever source the firmware
provides. Configurable via `CAMERA_SOURCE`:

  - Integer (e.g. `0`)              → cv2.VideoCapture(0)  — local USB / MIPI
  - `/dev/videoN`                    → same as above, by path
  - `rtsp://host:port/path`          → Unitree onboard RTSP server (G1 EDU
                                       publishes the front camera on the head Pi)
  - `http://host:port/stream.mjpg`   → some firmwares expose an MJPEG endpoint
  - GStreamer pipeline string ending in `appsink` → custom capture

A worker thread holds the capture open, decodes into BGR ndarrays,
and pushes the latest frame into a 1-slot queue. The HTTP
`/stream/color` endpoint pulls from that queue, JPEG-encodes, and
yields multipart chunks — same wire format as go2-camera, so go2-RC
or g1-RC can point at this with no client changes.

Endpoints:
  GET /health         → {"status": ..., "frames": <count>, "fps": ...}
  GET /stream/color   → multipart/x-mixed-replace MJPEG, latest-frame-wins
  POST /api/bark      → 503 (G1 audio path not implemented here)

Deliberately scoped: no audio, no perception WS, no skill control.
Those need separate paths on G1 (mic via ALSA, lidar via DDS through
unitree_sdk2_python) and don't belong in a camera bridge.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import cv2
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse


CAMERA_SOURCE = os.environ.get("CAMERA_SOURCE", "0")
PORT = int(os.environ.get("PORT", "8000"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "80"))
CAPTURE_WIDTH = int(os.environ.get("CAPTURE_WIDTH", "0"))   # 0 → leave default
CAPTURE_HEIGHT = int(os.environ.get("CAPTURE_HEIGHT", "0"))
CAPTURE_FPS = int(os.environ.get("CAPTURE_FPS", "0"))
RECONNECT_BACKOFF_S = 2.0
HEALTH_STALE_S = 5.0

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("g1-camera")


class RecentEventsHandler(logging.Handler):
    def __init__(self, limit: int = 80) -> None:
        super().__init__(logging.INFO)
        self.events: deque[dict[str, str]] = deque(maxlen=limit)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.events.append({
                "time": datetime.fromtimestamp(
                    record.created, timezone.utc
                ).isoformat(timespec="seconds"),
                "level": record.levelname.lower(),
                "service": "camera",
                "message": record.getMessage(),
            })
        except Exception:
            self.handleError(record)

    def snapshot(self) -> list[dict[str, str]]:
        return list(self.events)


recent_events = RecentEventsHandler()
logging.getLogger("g1-camera").addHandler(recent_events)


class CameraState:
    """Shared state between the capture thread and the FastAPI server.

    Latest-frame-wins: `frames` is a 1-slot queue, always overwritten
    on push. Same shape as go2-camera's CameraState so the MJPEG
    generator is identical.
    """

    def __init__(self) -> None:
        self.frames: "queue.Queue" = queue.Queue(maxsize=1)
        self.first_frame_logged = False
        self.frame_count = 0
        self.last_frame_t = 0.0
        self.fps = 0.0
        self._fps_window_t = time.monotonic()
        self._fps_window_count = 0
        self.started_at = time.monotonic()
        self.open_attempts = 0
        self.reconnects = 0
        self.consecutive_failures = 0
        self.error_code: str | None = None
        self.error: str | None = None
        self.hint: str | None = None
        self.error_at: float | None = None
        self._last_error_log_at = 0.0
        self._last_open_log_at = 0.0

    def set_error(self, code: str, message: str, hint: str) -> None:
        changed = code != self.error_code or message != self.error
        self.error_code = code
        self.error = message
        self.hint = hint
        self.error_at = time.monotonic()
        now = time.monotonic()
        if changed or now - self._last_error_log_at >= 30.0:
            log.warning(
                "camera_error code=%s message=%s hint=%s",
                code,
                message,
                hint,
            )
            self._last_error_log_at = now

    def clear_error(self) -> None:
        if self.error_code is not None:
            log.info("camera_recovered previous_error=%s", self.error_code)
        self.error_code = None
        self.error = None
        self.hint = None
        self.error_at = None

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


def _coerce_source(raw: str):
    """`cv2.VideoCapture` takes either an int (device index) or a string
    (path, URL, or GStreamer pipeline). Convert env var to the right
    type so OpenCV picks the right backend."""
    if raw.isdigit():
        return int(raw)
    return raw


def _open_capture():
    src = _coerce_source(CAMERA_SOURCE)
    now = time.monotonic()
    if state.open_attempts == 1 or now - state._last_open_log_at >= 30.0:
        log.info("Opening VideoCapture source=%r attempt=%d", src, state.open_attempts)
        state._last_open_log_at = now
    cap = cv2.VideoCapture(src)
    if CAPTURE_WIDTH > 0:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_WIDTH)
    if CAPTURE_HEIGHT > 0:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_HEIGHT)
    if CAPTURE_FPS > 0:
        cap.set(cv2.CAP_PROP_FPS, CAPTURE_FPS)
    return cap


def _capture_loop() -> None:
    """Hold the capture open, push frames into state, reopen on failure.

    OpenCV's RTSP/USB backends can return False from `read()` for a
    variety of transient reasons (network glitch, USB unplug). We retry
    by reopening rather than tight-looping on a dead capture — a fresh
    handle clears any latched error state in the backend.
    """
    while True:
        state.open_attempts += 1
        try:
            cap = _open_capture()
        except Exception as exc:
            state.set_error(
                "camera_open_exception",
                f"{type(exc).__name__}: {exc}",
                "Check CAMERA_SOURCE and run `wendy device hardware list` "
                "to confirm the camera is exposed to the app.",
            )
            log.exception("VideoCapture raised while opening source=%r", CAMERA_SOURCE)
            time.sleep(RECONNECT_BACKOFF_S)
            continue
        if not cap.isOpened():
            state.set_error(
                "camera_open_failed",
                f"OpenCV could not open CAMERA_SOURCE={CAMERA_SOURCE!r}",
                "The G1 D435i color node is commonly /dev/video4, but it can "
                "change. Check the device camera list and try the color node.",
            )
            time.sleep(RECONNECT_BACKOFF_S)
            continue

        log.info(
            "VideoCapture opened: %dx%d @ %.1f fps (reported by backend)",
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            float(cap.get(cv2.CAP_PROP_FPS)),
        )

        consecutive_failures = 0
        state.clear_error()
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                consecutive_failures += 1
                state.consecutive_failures = consecutive_failures
                if consecutive_failures >= 30:
                    state.set_error(
                        "camera_read_failed",
                        "Camera opened but returned 30 empty frames.",
                        "Another app may own the camera, or CAMERA_SOURCE may "
                        "point at an IR/depth node. Stop other camera apps and "
                        "select the D435i color node.",
                    )
                    state.reconnects += 1
                    break
                time.sleep(0.05)
                continue
            consecutive_failures = 0
            state.consecutive_failures = 0
            if not state.first_frame_logged:
                state.first_frame_logged = True
                log.info(
                    "First frame: %dx%d", frame.shape[1], frame.shape[0]
                )
            state.push(frame)
            state.clear_error()

        try:
            cap.release()
        except Exception as exc:
            log.debug("VideoCapture release failed: %s", exc)
        time.sleep(RECONNECT_BACKOFF_S)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info(
        "startup source=%r requested=%sx%s@%s jpeg_quality=%d",
        CAMERA_SOURCE,
        CAPTURE_WIDTH or "auto",
        CAPTURE_HEIGHT or "auto",
        CAPTURE_FPS or "auto",
        JPEG_QUALITY,
    )
    threading.Thread(target=_capture_loop, name="g1-cap", daemon=True).start()
    yield


app = FastAPI(title="g1-camera", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> JSONResponse:
    healthy = state.first_frame_logged and (
        time.monotonic() - state.last_frame_t < HEALTH_STALE_S
    )
    status = "ok" if healthy else "error" if state.error else "starting"
    return JSONResponse(
        {
            "ok": healthy,
            "status": status,
            "frames": state.frame_count,
            "fps": round(state.fps, 1),
            "source": CAMERA_SOURCE,
            "error": state.error,
            "error_code": state.error_code,
            "hint": state.hint,
        },
        status_code=200 if healthy else 503,
    )


@app.get("/diagnostics")
async def diagnostics() -> dict:
    age = (
        round(time.monotonic() - state.last_frame_t, 1)
        if state.last_frame_t
        else None
    )
    healthy = state.first_frame_logged and age is not None and age < HEALTH_STALE_S
    return {
        "service": "camera",
        "status": "ready" if healthy else "failed" if state.error else "starting",
        "ok": healthy,
        "uptime_seconds": round(time.monotonic() - state.started_at, 1),
        "error": state.error,
        "error_code": state.error_code,
        "hint": state.hint,
        "checks": {
            "capture_opened": state.first_frame_logged,
            "frame_fresh": healthy,
            "last_frame_age_seconds": age,
        },
        "metrics": {
            "frames": state.frame_count,
            "fps": round(state.fps, 1),
            "open_attempts": state.open_attempts,
            "reconnects": state.reconnects,
            "consecutive_failures": state.consecutive_failures,
        },
        "config": {
            "source": CAMERA_SOURCE,
            "width": CAPTURE_WIDTH or "auto",
            "height": CAPTURE_HEIGHT or "auto",
            "fps": CAPTURE_FPS or "auto",
            "jpeg_quality": JPEG_QUALITY,
        },
        "events": recent_events.snapshot(),
    }


def _mjpeg_generator():
    """Yields multipart MJPEG chunks; blocks-with-timeout when there's no new frame.

    Same wire format as go2-camera so consumers (g1-RC, go2-RC) work
    interchangeably.
    """
    boundary = b"--frame"
    while True:
        try:
            img = state.frames.get(timeout=2.0)
        except queue.Empty:
            continue
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            state.set_error(
                "jpeg_encode_failed",
                "OpenCV could not encode the latest camera frame as JPEG.",
                "Inspect camera service logs for the OpenCV encoder error.",
            )
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


@app.get("/stream/color")
def stream_color() -> StreamingResponse:
    return StreamingResponse(
        _mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.post("/api/bark")
async def bark() -> JSONResponse:
    """G1 audio playback isn't wired here.

    Returning 503 so g1-RC's /api/bark proxy degrades cleanly: the UI
    keeps the camera + joystick working and just doesn't trigger a
    speaker sound. To enable bark on G1 you'd need to play a WAV out
    of the head Pi's ALSA device, which is firmware-dependent and
    belongs in a separate audio service.
    """
    return JSONResponse(
        {"ok": False, "reason": "g1-camera does not implement audio"},
        status_code=503,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        access_log=False,
    )
