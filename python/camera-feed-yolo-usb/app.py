#!/usr/bin/env python3
"""YOLOv8 on a USB camera, ONNX Runtime on the CPU.

Two choices here are the point of this template:

1. **Motion-JPEG is requested explicitly.** On a USB 2.0 port -- which is what
   many boards give you, including the Qualcomm Dragonwing IQ-8275, whose only
   host port carries a usb2-phy -- raw YUYV at 720p30 needs ~440 Mbit/s and
   does not fit in 480. The camera then quietly negotiates down to something
   small and the picture looks like mush, with no error anywhere. Asking for
   MJPEG costs about 15 Mbit/s for the same picture, because the camera
   compresses it itself. _negotiated() reports what actually happened, since
   V4L2 accepts a request and then gives you whatever it likes.

2. **Inference is ONNX Runtime, not ultralytics**, so torch never enters the
   runtime image: ~400 MB instead of ~3 GB. Torch is needed once, to export
   the model, and that happens in a build stage that is thrown away.

Env: YOLO_DEVICE, YOLO_MAX_FPS, YOLO_CONF, YOLO_WIDTH, YOLO_HEIGHT, YOLO_IMGSZ.
"""
from __future__ import annotations

import asyncio
import ast
import fcntl
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("yolo-camera")

MODEL = Path(__file__).parent / "yolov8n.onnx"
IMGSZ = int(os.environ.get("YOLO_IMGSZ", "320"))
WIDTH = int(os.environ.get("YOLO_WIDTH", "1280"))
HEIGHT = int(os.environ.get("YOLO_HEIGHT", "720"))
MAX_FPS = float(os.environ.get("YOLO_MAX_FPS", "10"))
# Adjustable from the page's slider, so it is a mutable box rather than a
# constant: detect() reads it per frame.
CONF = {"value": float(os.environ.get("YOLO_CONF", "0.25"))}
IOU = 0.45


# --------------------------------------------------------------------------
# camera
# --------------------------------------------------------------------------
# A UVC camera claims more than one /dev/video* node -- the C920 takes video2
# and video3 -- and only the first is a capture device. The rest are metadata
# nodes that open successfully and then never yield a frame, which is exactly
# the retry storm fixed in templates#104. Ask the driver instead of guessing.
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
VIDIOC_QUERYCAP = 0x80685600


def _query(path: str) -> tuple[bool, str]:
    """(is a capture node, driver name) straight from VIDIOC_QUERYCAP."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return False, ""
    try:
        buf = bytearray(104)
        fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf, True)
        driver = buf[0:16].split(b"\x00")[0].decode(errors="replace")
        caps = int.from_bytes(buf[84:88], sys.byteorder)
        device_caps = int.from_bytes(buf[88:92], sys.byteorder)
        # device_caps describes this node; caps describes the whole physical
        # device, so a metadata node would pass on caps alone.
        return bool((device_caps or caps) & V4L2_CAP_VIDEO_CAPTURE), driver
    except OSError:
        return False, ""
    finally:
        os.close(fd)


def _is_capture(path: str) -> bool:
    return _query(path)[0]


def _find_camera() -> str | None:
    forced = os.environ.get("YOLO_DEVICE")
    if forced:
        return forced if _is_capture(forced) else None
    nodes = sorted(Path("/dev").glob("video*"),
                   key=lambda p: int("".join(c for c in p.name if c.isdigit()) or 0))
    for node in nodes:
        capture, driver = _query(str(node))
        if not capture:
            continue
        # A uvcvideo node that reports capture is a real camera; take it without
        # a probe read, which costs a full open/close at the default format.
        # Anything else gets checked, because the SoC's qcom-iris codec blocks
        # register nodes that open happily and never yield a frame.
        if driver == "uvcvideo":
            return str(node)
        probe = cv2.VideoCapture(str(node), cv2.CAP_V4L2)
        ok = probe.isOpened() and probe.read()[0]
        probe.release()
        if ok:
            return str(node)
        log.info("%s (%s) reports capture but yields no frame -- skipping", node, driver)
    return None


def _fourcc(cap) -> str:
    v = int(cap.get(cv2.CAP_PROP_FOURCC))
    return "".join(chr((v >> (8 * i)) & 0xFF) for i in range(4)).strip() or "?"


def _open(path: str):
    cap = cv2.VideoCapture(path, cv2.CAP_V4L2)
    if not cap.isOpened():
        return None
    # Order matters: the pixel format has to be set before the frame size, or
    # V4L2 sizes the raw format and then ignores the codec change.
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, 30)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def _negotiated(cap) -> dict:
    return {
        "fourcc": _fourcc(cap),
        "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        "fps": round(cap.get(cv2.CAP_PROP_FPS), 1),
    }


# --------------------------------------------------------------------------
# inference
# --------------------------------------------------------------------------
_sess = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
_input = _sess.get_inputs()[0].name
try:
    # ultralytics stamps the class names into the ONNX metadata at export, so
    # they always match the weights -- unlike a hardcoded COCO list.
    NAMES = ast.literal_eval(_sess.get_modelmeta().custom_metadata_map["names"])
except Exception:
    NAMES = {i: str(i) for i in range(80)}
log.info("model ready: %s, imgsz=%d, %d classes", MODEL.name, IMGSZ, len(NAMES))


def _letterbox(frame):
    """Resize preserving aspect ratio and pad to a square, returning the scale
    and padding so boxes can be mapped back to the original frame."""
    h, w = frame.shape[:2]
    scale = min(IMGSZ / w, IMGSZ / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((IMGSZ, IMGSZ, 3), 114, dtype=np.uint8)
    dx, dy = (IMGSZ - nw) // 2, (IMGSZ - nh) // 2
    canvas[dy:dy + nh, dx:dx + nw] = resized
    return canvas, scale, dx, dy


def detect(frame):
    canvas, scale, dx, dy = _letterbox(frame)
    blob = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0

    out = _sess.run(None, {_input: blob})[0]          # (1, 84, N)
    pred = np.squeeze(out, 0).T                        # (N, 84)

    conf = CONF["value"]
    scores = pred[:, 4:].max(axis=1)
    keep = scores > conf
    if not keep.any():
        return []
    pred, scores = pred[keep], scores[keep]
    class_ids = pred[:, 4:].argmax(axis=1)

    # cx,cy,w,h in letterboxed space -> x,y,w,h in the original frame
    cx, cy, bw, bh = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
    x = (cx - bw / 2 - dx) / scale
    y = (cy - bh / 2 - dy) / scale
    boxes = np.stack([x, y, bw / scale, bh / scale], axis=1)

    idx = cv2.dnn.NMSBoxes(boxes.tolist(), scores.tolist(), conf, IOU)
    if len(idx) == 0:
        return []
    return [
        {"box": boxes[i].tolist(), "conf": float(scores[i]),
         "name": NAMES.get(int(class_ids[i]), str(class_ids[i]))}
        for i in np.array(idx).flatten()
    ]


_PALETTE = [(56, 168, 96), (232, 132, 48), (66, 135, 245), (200, 64, 128),
            (240, 196, 32), (128, 96, 220), (32, 188, 196), (220, 80, 72)]


def annotate(frame, dets):
    for d in dets:
        x, y, w, h = (int(v) for v in d["box"])
        colour = _PALETTE[sum(d["name"].encode()) % len(_PALETTE)]
        cv2.rectangle(frame, (x, y), (x + w, y + h), colour, 2)
        label = f'{d["name"]} {d["conf"]:.2f}'
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(frame, (x, y - th - 6), (x + tw + 6, y), colour, -1)
        cv2.putText(frame, label, (x + 3, y - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return frame


# --------------------------------------------------------------------------
# capture loop
# --------------------------------------------------------------------------
_latest: bytes | None = None
_lock = threading.Lock()
_state: dict = {"camera": None, "format": None, "error": "starting", "detections": [],
                "inference_ms": 0.0, "capture_fps": 0.0}


def capture_loop():
    global _latest
    interval = 1.0 / MAX_FPS if MAX_FPS > 0 else 0
    backoff = 2.0
    while True:
        path = _find_camera()
        if path is None:
            _state.update(error="no capture device -- is the camera in the micro-USB port "
                                "with an OTG adapter?", camera=None, format=None)
            log.warning(_state["error"])
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue

        cap = _open(path)
        if cap is None:
            _state.update(error=f"{path} could not be opened", camera=path)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue

        fmt = _negotiated(cap)
        _state.update(camera=path, format=fmt, error=None)
        log.info("capture %s -> %s %dx%d @%.0f", path, fmt["fourcc"],
                 fmt["width"], fmt["height"], fmt["fps"])
        if fmt["fourcc"] != "MJPG":
            log.warning("camera gave %s, not MJPG -- expect a low-resolution picture "
                        "on this board's USB 2.0 host port", fmt["fourcc"])
        backoff = 2.0

        last, frames, t_fps = 0.0, 0, time.monotonic()
        while True:
            ok, frame = cap.read()
            if not ok:
                log.warning("capture read failed; reopening")
                break
            frames += 1
            now = time.monotonic()
            if now - t_fps >= 1.0:
                _state["capture_fps"] = round(frames / (now - t_fps), 1)
                frames, t_fps = 0, now

            if now - last < interval:
                continue
            last = now

            t0 = time.monotonic()
            try:
                dets = detect(frame)
            except Exception:
                log.warning("inference failed", exc_info=True)
                continue
            _state["inference_ms"] = round((time.monotonic() - t0) * 1000, 1)
            _state["detections"] = dets

            ok, buf = cv2.imencode(".jpg", annotate(frame, dets),
                                   [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ok:
                with _lock:
                    _latest = buf.tobytes()
        cap.release()


threading.Thread(target=capture_loop, daemon=True).start()

app = FastAPI()

_assets = Path(__file__).parent / "assets"
if _assets.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_assets)), name="assets")


@app.get("/")
async def root():
    return FileResponse(Path(__file__).parent / "index.html", media_type="text/html")


@app.get("/status")
async def status():
    return JSONResponse({
        "camera": _state["camera"],
        "format": _state["format"],
        "error": _state["error"],
        "capture_fps": _state["capture_fps"],
        "inference_ms": _state["inference_ms"],
        "detections": [{"name": d["name"], "conf": round(d["conf"], 2)}
                       for d in _state["detections"]],
        "backend": "onnxruntime-cpu",
        "imgsz": IMGSZ,
    })


async def _read_commands(ws: WebSocket):
    """The page's only upstream message is a new confidence threshold."""
    try:
        while True:
            msg = await ws.receive_text()
            try:
                value = float(json.loads(msg).get("confidence"))
            except (ValueError, TypeError, AttributeError):
                continue
            CONF["value"] = max(0.01, min(0.99, value))
    except (WebSocketDisconnect, RuntimeError):
        pass


@app.websocket("/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    commands = asyncio.create_task(_read_commands(ws))
    sent = None
    try:
        while True:
            with _lock:
                frame = _latest
            if frame is not None and frame is not sent:
                await ws.send_bytes(frame)
                await ws.send_text(json.dumps({
                    "detections": [{"name": d["name"], "conf": round(d["conf"], 2)}
                                   for d in _state["detections"]],
                    "inference_ms": _state["inference_ms"],
                    "capture_fps": _state["capture_fps"],
                    "format": _state["format"],
                }))
                sent = frame
            await asyncio.sleep(0.03)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        commands.cancel()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "{{.PORT}}")))
