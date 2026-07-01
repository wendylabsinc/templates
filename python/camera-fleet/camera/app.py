"""usb-camera: stream a USB/UVC RGB webcam as MJPEG over HTTP.

Captures a V4L2 device with OpenCV in a background thread (latest-frame-wins)
and re-serves it as a standard multipart MJPEG stream — viewable directly in any
browser, embeddable in an <img>, and drop-in compatible with go2-rc's camera
proxy (which expects `${URL}/stream/color`). No WebRTC, no AES key.

  GET /              live viewer page
  GET /stream        multipart/x-mixed-replace MJPEG
  GET /stream/color  alias of /stream (go2-rc CAMERA_UPSTREAM_URL compatible)
  GET /health        {"status","frames","fps","device","resolution","error"}

All config via env (set by the Dockerfile from template vars):
  PORT VIDEO_DEVICE WIDTH HEIGHT FPS JPEG_QUALITY
"""
import os
import threading
import time
from contextlib import asynccontextmanager

import cv2
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

PORT = int(os.environ.get("PORT", "8000"))
DEVICE = os.environ.get("VIDEO_DEVICE", "/dev/video0")
WIDTH = int(os.environ.get("WIDTH", "1280"))
HEIGHT = int(os.environ.get("HEIGHT", "720"))
FPS = int(os.environ.get("FPS", "30"))
QUALITY = int(os.environ.get("JPEG_QUALITY", "80"))

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Start the capture thread on startup (replaces the deprecated
    # @app.on_event("startup") hook).
    threading.Thread(target=_capture_loop, daemon=True).start()
    yield


app = FastAPI(title="usb-camera", lifespan=lifespan)

# Shared latest-frame state between the capture thread and HTTP handlers.
_state = {"frame": None, "count": 0, "fps": 0.0, "ok": False, "err": "starting"}
_lock = threading.Lock()


def _open_capture():
    # Accept either a device path (/dev/video0) or a numeric index ("0").
    dev = int(DEVICE) if str(DEVICE).isdigit() else DEVICE
    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    # Ask the cam for MJPG: most UVC webcams only hit high res/fps in MJPG, and
    # it keeps USB bandwidth sane vs raw YUYV.
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, FPS)
    # Keep only the newest frame in the driver buffer — otherwise OpenCV hands
    # back queued (stale) frames and adds ~100-200ms of latency.
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:  # noqa: BLE001
        pass
    return cap


def _capture_loop():
    enc = [int(cv2.IMWRITE_JPEG_QUALITY), QUALITY]
    cap = None
    win_t0 = time.time()
    win_n0 = 0
    while True:
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
            cap = _open_capture()
            if not cap.isOpened():
                with _lock:
                    _state["ok"] = False
                    _state["err"] = f"cannot open {DEVICE}"
                time.sleep(2.0)
                continue
        ok, frame = cap.read()
        if not ok or frame is None:
            with _lock:
                _state["ok"] = False
                _state["err"] = "read failed (camera unplugged?)"
            cap.release()
            cap = None
            time.sleep(0.5)
            continue
        ok2, buf = cv2.imencode(".jpg", frame, enc)
        if not ok2:
            continue
        now = time.time()
        with _lock:
            _state["frame"] = buf.tobytes()
            _state["count"] += 1
            _state["ok"] = True
            _state["err"] = ""
            if now - win_t0 >= 1.0:
                _state["fps"] = round((_state["count"] - win_n0) / (now - win_t0), 1)
                win_t0 = now
                win_n0 = _state["count"]


@app.get("/health")
def health() -> dict:
    with _lock:
        return {
            "status": "ok" if _state["ok"] else "starting",
            "frames": _state["count"],
            "fps": _state["fps"],
            "device": DEVICE,
            "resolution": f"{WIDTH}x{HEIGHT}",
            "error": _state["err"],
        }


def _mjpeg():
    """Yield multipart MJPEG, latest-frame-wins. Sleeps when no new frame so a
    dead/booting camera just stalls the stream rather than busy-looping."""
    last = -1
    interval = 1.0 / max(FPS, 1)
    while True:
        with _lock:
            frame = _state["frame"]
            count = _state["count"]
        if frame is None or count == last:
            time.sleep(interval)
            continue
        last = count
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
            + frame + b"\r\n"
        )


def _stream_response() -> StreamingResponse:
    return StreamingResponse(
        _mjpeg(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/stream")
def stream() -> StreamingResponse:
    return _stream_response()


@app.get("/stream/color")
def stream_color() -> StreamingResponse:
    # Alias so this drops straight into go2-rc (CAMERA_UPSTREAM_URL=.../stream/color).
    return _stream_response()


PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>USB Camera</title>
<style>
  body{margin:0;background:#0c0e12;color:#e7ebf0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
       min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;padding:20px;}
  h1{font-size:18px;font-weight:700;margin:0;letter-spacing:-.01em;}
  .feed{max-width:100%;border:1px solid #262c36;border-radius:14px;background:#000;line-height:0;
        box-shadow:0 10px 40px rgba(0,0,0,.5);overflow:hidden;}
  .feed img{display:block;max-width:100%;height:auto;}
  .meta{font-size:13px;color:#9aa6b2;}
  .meta b{color:#2dd4bf;}
</style></head><body>
  <h1>📷 USB Camera</h1>
  <div class="feed"><img src="/stream" alt="camera feed"></div>
  <div class="meta" id="meta">connecting…</div>
<script>
  async function tick(){
    try{
      const h = await (await fetch("/health")).json();
      document.getElementById("meta").innerHTML =
        h.status==="ok"
          ? "● <b>live</b> · "+h.resolution+" · "+h.fps+" fps · "+h.device
          : "○ "+(h.error||h.status)+" · "+h.device;
    }catch(e){ document.getElementById("meta").textContent="server offline"; }
  }
  tick(); setInterval(tick, 2000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info", access_log=False)
