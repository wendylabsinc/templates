#!/usr/bin/env python3
"""
Webcam streaming server.
GStreamer MJPEG-over-WebSocket — camera outputs MJPEG natively on most USB
webcams so no encoding is needed. Frames are sent as-is to connected clients.
"""
import asyncio
import collections
import glob
import json
import logging
import platform
import subprocess
import threading
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")

from gi.repository import Gst, GLib
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

_log_buffer = collections.deque(maxlen=200)


class _BufferHandler(logging.Handler):
    def emit(self, record):
        _log_buffer.append(self.format(record))


logging.basicConfig(level=logging.INFO)
_bh = _BufferHandler()
_bh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(_bh)
logger = logging.getLogger(__name__)

Gst.init(None)

_glib_loop = GLib.MainLoop()
threading.Thread(target=_glib_loop.run, daemon=True).start()

app = FastAPI()

_app_dir = Path(__file__).parent
_assets_dir = _app_dir / "assets"
if _assets_dir.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

IS_MACOS = platform.system() == "Darwin"


def _v4l2_device_name(path: str) -> str:
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", "--device", path, "--info"],
            stderr=subprocess.DEVNULL, timeout=2,
        ).decode()
        for line in out.splitlines():
            if "Card type" in line:
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return Path(path).name


def _v4l2_is_capture(path: str) -> bool:
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", "--device", path, "--all"],
            stderr=subprocess.DEVNULL, timeout=2,
        ).decode()
        return "Video Capture" in out
    except Exception:
        return False


def enumerate_cameras() -> list[dict]:
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Video/Source", Gst.Caps.from_string("video/x-raw"))
    monitor.start()
    devices = monitor.get_devices()

    cameras = []
    for i, dev in enumerate(devices):
        props = dev.get_properties()
        name = dev.get_display_name()
        if IS_MACOS:
            idx = props.get_int("device.index")
            device_id = str(idx.value) if idx[0] else str(i)
        else:
            path = props.get_string("device.path") or props.get_string("api.v4l2.path")
            device_id = path if path else f"/dev/video{i}"
        cameras.append({"id": device_id, "name": name})
    monitor.stop()

    if not cameras and not IS_MACOS:
        for path in sorted(glob.glob("/dev/video*")):
            if _v4l2_is_capture(path):
                cameras.append({"id": path, "name": _v4l2_device_name(path)})
        if cameras:
            logger.info("Discovered %d camera(s) via /dev/video*", len(cameras))

    return cameras


def build_source(device_id: str | None = None) -> str:
    if IS_MACOS:
        src = "avfvideosrc"
        if device_id is not None:
            src += f" device-index={device_id}"
    else:
        src = f"v4l2src device={device_id or '/dev/video0'}"
    return src


class MJPEGCamera:
    """Captures MJPEG frames from a camera using GStreamer appsink.

    The camera outputs MJPEG natively on most USB webcams, so no
    encoding is needed. Frames are sent as-is to connected WebSocket clients.
    """

    def __init__(self):
        self.pipeline = None
        self.queues: dict[WebSocket, asyncio.Queue] = {}
        self._lock = threading.Lock()
        self._current_device: str | None = None
        self._loop = None

    def _start_pipeline(self, device_id: str | None = None) -> Gst.Pipeline | None:
        src = build_source(device_id)

        appsink = "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
        pipelines = [
            # MJPEG native — most permissive first
            f"{src} ! image/jpeg ! {appsink}",
            f"{src} ! image/jpeg,width=640,height=480 ! {appsink}",
            # Camera outputs raw → encode to JPEG
            f"{src} ! videoconvert ! jpegenc quality=70 ! {appsink}",
        ]

        for p_str in pipelines:
            try:
                pipeline = Gst.parse_launch(p_str)
                ret = pipeline.set_state(Gst.State.PAUSED)
                if ret == Gst.StateChangeReturn.FAILURE:
                    pipeline.set_state(Gst.State.NULL)
                    logger.info("Pipeline failed: %s", p_str)
                    continue
                if ret == Gst.StateChangeReturn.ASYNC:
                    ret, _, _ = pipeline.get_state(5 * Gst.SECOND)
                    if ret == Gst.StateChangeReturn.FAILURE:
                        pipeline.set_state(Gst.State.NULL)
                        logger.info("Pipeline preroll failed: %s", p_str)
                        continue
                logger.info("Pipeline ready: %s", p_str)
                return pipeline
            except Exception as e:
                logger.info("Pipeline exception: %s — %s", p_str, e)
        return None

    def _on_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if not sample:
            logger.warning("pull-sample returned None")
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            logger.warning("buffer map failed")
            return Gst.FlowReturn.OK
        data = bytes(mapinfo.data)
        buf.unmap(mapinfo)
        if not hasattr(self, '_frame_count'):
            self._frame_count = 0
        self._frame_count += 1
        if self._frame_count <= 3 or self._frame_count % 100 == 0:
            logger.info("Frame %d: %d bytes, %d queues", self._frame_count, len(data), len(self.queues))

        with self._lock:
            for q in self.queues.values():
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    pass

        return Gst.FlowReturn.OK

    async def add_client(self, ws: WebSocket) -> asyncio.Queue:
        self._loop = asyncio.get_running_loop()
        q = asyncio.Queue(maxsize=2)
        with self._lock:
            if not self.pipeline:
                self.pipeline = self._start_pipeline(self._current_device)
                if not self.pipeline:
                    raise RuntimeError("Could not start camera pipeline")
                sink = self.pipeline.get_by_name("sink")
                sink.connect("new-sample", self._on_new_sample)
                self.pipeline.set_state(Gst.State.PLAYING)
                logger.info("Camera streaming started")
            self.queues[ws] = q
        logger.info("Client added (total: %d)", len(self.queues))
        return q

    def remove_client(self, ws: WebSocket):
        with self._lock:
            self.queues.pop(ws, None)
            if not self.queues and self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None
                logger.info("Camera stopped (no clients)")
        logger.info("Client removed (total: %d)", len(self.queues))

    async def switch_camera(self, device_id: str):
        with self._lock:
            if self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None
                self._frame_count = 0
            self._current_device = device_id
            self.pipeline = self._start_pipeline(device_id)
            if not self.pipeline:
                raise RuntimeError(f"Could not start camera {device_id}")
            sink = self.pipeline.get_by_name("sink")
            sink.connect("new-sample", self._on_new_sample)
            self.pipeline.set_state(Gst.State.PLAYING)
        logger.info("Switched to camera %s", device_id)


camera = MJPEGCamera()


@app.get("/cameras")
async def list_cameras():
    return JSONResponse(content=enumerate_cameras())


@app.websocket("/stream")
async def websocket_stream(websocket: WebSocket):
    await websocket.accept()
    try:
        q = await camera.add_client(websocket)
    except Exception as e:
        logger.error(f"Failed to start camera: {e}")
        await websocket.close(code=1011)
        return

    async def send_frames():
        try:
            while True:
                data = await q.get()
                await websocket.send_bytes(data)
        except Exception:
            pass

    async def recv_commands():
        try:
            while True:
                msg = json.loads(await websocket.receive_text())
                if "switch_camera" in msg:
                    try:
                        await camera.switch_camera(msg["switch_camera"])
                    except Exception as e:
                        logger.error(f"Camera switch failed: {e}")
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    try:
        done, pending = await asyncio.wait(
            [asyncio.create_task(send_frames()), asyncio.create_task(recv_commands())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        camera.remove_client(websocket)


@app.get("/logs")
async def get_logs():
    return JSONResponse(content=list(_log_buffer))


@app.get("/debug")
async def debug_info():
    cameras = enumerate_cameras()
    return JSONResponse(content={
        "mode": "mjpeg-ws",
        "cameras": cameras,
        "pipeline_state": camera.pipeline.get_state(0)[1].value_nick if camera.pipeline else None,
        "num_clients": len(camera.queues),
    })


@app.get("/")
async def root():
    return FileResponse(Path(__file__).parent / "index.html", media_type="text/html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port={{.PORT}})
