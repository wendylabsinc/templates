"""UVC camera -> MJPEG service for the rc-car app group.

Captures the car's generic USB (UVC) camera with OpenCV/V4L2 and serves it as a
multipart MJPEG stream at /stream/color — the same endpoint shape the Go2
`camera` service exposed, so the `rc` teleop UI proxies it unchanged.

The car's camera is a generic UVC device (not a RealSense), so plain V4L2 works.
"""
import logging
import os
import sys
import threading
import time

import cv2
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("camera")

PORT = int(os.environ.get("PORT", "8000"))
DEVICE = os.environ.get("CAMERA_DEVICE", "/dev/video0")
WIDTH = int(os.environ.get("CAMERA_WIDTH", "640"))
HEIGHT = int(os.environ.get("CAMERA_HEIGHT", "480"))
FPS = int(os.environ.get("CAMERA_FPS", "20"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "80"))
# Generic UVC cams default to raw YUYV, which OpenCV often misreads over a
# bandwidth-limited USB-2 link (garbled green/noise bands). Forcing MJPG makes
# the camera deliver compressed JPEG that OpenCV decodes cleanly. Set to ""
# to disable and use the camera's default format.
FOURCC = os.environ.get("CAMERA_FOURCC", "MJPG")

app = FastAPI(title="rc-car-camera")
_info = {}

_frame = None
_lock = threading.Lock()
_running = True


def _capture_loop():
    global _frame
    while _running:
        cap = cv2.VideoCapture(DEVICE, cv2.CAP_V4L2)
        # FOURCC must be set before width/height so the camera negotiates the
        # right mode. MJPG avoids the raw-YUYV misread that produces banding.
        if FOURCC:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*FOURCC))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
        cap.set(cv2.CAP_PROP_FPS, FPS)
        if not cap.isOpened():
            logger.warning("could not open %s; retrying in 2s", DEVICE)
            time.sleep(2)
            continue
        # Read back what the driver actually negotiated.
        fcc = int(cap.get(cv2.CAP_PROP_FOURCC))
        _info.update({
            "fourcc": "".join(chr((fcc >> (8 * i)) & 0xFF) for i in range(4)),
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": cap.get(cv2.CAP_PROP_FPS),
        })
        logger.info("camera %s opened %s", DEVICE, _info)
        while _running:
            ok, img = cap.read()
            if not ok:
                logger.warning("frame read failed; reopening %s", DEVICE)
                break
            ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ok:
                with _lock:
                    _frame = jpg.tobytes()
        cap.release()
        time.sleep(0.5)


def _mjpeg():
    boundary = b"--frame"
    while True:
        with _lock:
            buf = _frame
        if buf is None:
            time.sleep(0.05)
            continue
        yield boundary + b"\r\nContent-Type: image/jpeg\r\nContent-Length: " + \
            str(len(buf)).encode() + b"\r\n\r\n" + buf + b"\r\n"
        time.sleep(1.0 / max(1, FPS))


@app.on_event("startup")
def _startup():
    threading.Thread(target=_capture_loop, daemon=True).start()


@app.get("/stream/color")
def stream_color():
    return StreamingResponse(_mjpeg(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/health")
def health():
    with _lock:
        have = _frame is not None
    return JSONResponse({"ok": True, "device": DEVICE, "streaming": have})


@app.get("/info")
def info():
    return JSONResponse({"device": DEVICE, "requested_fourcc": FOURCC, "negotiated": _info})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
