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
    log.info("Opening VideoCapture source=%r", src)
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
        cap = _open_capture()
        if not cap.isOpened():
            log.warning(
                "VideoCapture failed to open %r; retrying in %.1fs",
                CAMERA_SOURCE, RECONNECT_BACKOFF_S,
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
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                consecutive_failures += 1
                if consecutive_failures >= 30:
                    log.warning(
                        "30 consecutive read failures; reopening capture",
                    )
                    break
                time.sleep(0.05)
                continue
            consecutive_failures = 0
            if not state.first_frame_logged:
                state.first_frame_logged = True
                log.info(
                    "First frame: %dx%d", frame.shape[1], frame.shape[0]
                )
            state.push(frame)

        try:
            cap.release()
        except Exception:
            pass
        time.sleep(RECONNECT_BACKOFF_S)


app = FastAPI(title="g1-camera", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    threading.Thread(target=_capture_loop, name="g1-cap", daemon=True).start()


@app.get("/health")
async def health() -> JSONResponse:
    healthy = state.first_frame_logged and (
        time.monotonic() - state.last_frame_t < HEALTH_STALE_S
    )
    return JSONResponse({
        "status": "ok" if healthy else "starting",
        "frames": state.frame_count,
        "fps": round(state.fps, 1),
        "source": CAMERA_SOURCE,
    })


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

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
