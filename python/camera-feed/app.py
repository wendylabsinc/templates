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
V4L_SYMLINK_DIRS = (Path("/dev/v4l/by-id"), Path("/dev/v4l/by-path"))


def _sysfs_video_node_path(path: str) -> Path:
    return Path("/sys/class/video4linux") / Path(path).name


def _v4l2_node_index(path: str) -> int:
    try:
        return int((_sysfs_video_node_path(path) / "index").read_text().strip())
    except Exception:
        return 999


def _v4l2_device_name(path: str) -> str:
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", "--device", path, "--info"],
            stderr=subprocess.DEVNULL,
            timeout=2,
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
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode()
        in_device_caps = False
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("Device Caps"):
                in_device_caps = True
                continue
            if not in_device_caps:
                continue
            if not line.startswith((" ", "\t")):
                break
            if stripped in {"Video Capture", "Video Capture Multiplanar"}:
                return True
        return False
    except Exception:
        return False


def _usb_device_id_for_video_node(path: str) -> str | None:
    try:
        device_path = (_sysfs_video_node_path(path) / "device").resolve()
    except Exception:
        return None

    for current in [device_path] + list(device_path.parents):
        if (current / "idVendor").exists() and (current / "idProduct").exists():
            return current.name.split(":", 1)[0]
    return None


def _linux_symlink_video_nodes() -> list[str]:
    nodes: list[str] = []

    def add(path: str):
        if path not in nodes:
            nodes.append(path)

    by_id = V4L_SYMLINK_DIRS[0]
    if by_id.is_dir():
        for link in sorted(by_id.iterdir()):
            if not link.name.startswith("usb-"):
                continue
            try:
                target = link.resolve()
            except Exception:
                continue
            if target.name.startswith("video"):
                add(f"/dev/{target.name}")
    if nodes:
        logger.info("Stable V4L USB targets via /dev/v4l/by-id: %s", ", ".join(nodes))
        return nodes

    by_path = V4L_SYMLINK_DIRS[1]
    if by_path.is_dir():
        for link in sorted(by_path.iterdir()):
            if "-usb-" not in link.name and "-usbv" not in link.name:
                continue
            try:
                target = link.resolve()
            except Exception:
                continue
            if target.name.startswith("video"):
                add(f"/dev/{target.name}")
    if nodes:
        logger.info("Stable V4L USB targets via /dev/v4l/by-path: %s", ", ".join(nodes))
    return nodes


def _linux_candidate_video_nodes() -> list[str]:
    symlink_nodes = _linux_symlink_video_nodes()
    if symlink_nodes:
        return symlink_nodes

    nodes = sorted(glob.glob("/dev/video*"), key=lambda p: (_v4l2_node_index(p), p))
    if not nodes:
        logger.info("No raw V4L2 nodes available under /dev/video*")
        return nodes

    usb_nodes = [path for path in nodes if _usb_device_id_for_video_node(path)]
    if usb_nodes:
        logger.info("Falling back to raw USB V4L2 nodes: %s", ", ".join(usb_nodes))
        return usb_nodes

    logger.info("Falling back to raw V4L2 nodes: %s", ", ".join(nodes))
    return nodes


def _enumerate_linux_cameras() -> list[dict]:
    cameras = []
    for path in _linux_candidate_video_nodes():
        if _v4l2_is_capture(path):
            cameras.append({"id": path, "name": _v4l2_device_name(path)})
    if cameras:
        logger.info(
            "Discovered %d camera(s) via V4L2: %s",
            len(cameras),
            ", ".join(f"{camera['id']} ({camera['name']})" for camera in cameras),
        )
    else:
        logger.info("No capture-classified V4L2 cameras found")
    return cameras


def enumerate_cameras() -> list[dict]:
    if not IS_MACOS:
        return _enumerate_linux_cameras()

    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Video/Source", Gst.Caps.from_string("video/x-raw"))
    monitor.start()
    devices = monitor.get_devices()

    cameras = []
    for i, dev in enumerate(devices):
        props = dev.get_properties()
        name = dev.get_display_name()
        idx = props.get_int("device.index")
        device_id = str(idx.value) if idx[0] else str(i)
        cameras.append({"id": device_id, "name": name})
    monitor.stop()
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
    """Captures MJPEG frames from a camera using GStreamer appsink."""

    def __init__(self):
        self.pipeline = None
        self.queues: dict[WebSocket, asyncio.Queue] = {}
        self._lock = threading.Lock()
        self._current_device: str | None = None
        self._loop = None
        self._bus = None
        self._bus_watch_id = None
        self._restart_task: asyncio.Task | None = None

    def _start_pipeline(self, device_id: str | None = None) -> Gst.Pipeline | None:
        src = build_source(device_id)
        appsink = "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
        pipelines = [
            f"{src} ! image/jpeg ! jpegdec ! jpegenc quality=85 ! {appsink}",
            f"{src} ! image/jpeg,width=640,height=480 ! jpegdec ! jpegenc quality=85 ! {appsink}",
            f"{src} ! videoconvert ! jpegenc quality=70 ! {appsink}",
            f"{src} ! image/jpeg ! {appsink}",
            f"{src} ! image/jpeg,width=640,height=480 ! {appsink}",
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
                        bus = pipeline.get_bus()
                        msg = bus.pop_filtered(Gst.MessageType.ERROR)
                        if msg:
                            err, debug = msg.parse_error()
                            logger.info(
                                "Pipeline preroll error for %s: %s%s",
                                p_str,
                                err,
                                f" ({debug})" if debug else "",
                            )
                        pipeline.set_state(Gst.State.NULL)
                        logger.info("Pipeline preroll failed: %s", p_str)
                        continue
                logger.info("Pipeline ready: %s", p_str)
                return pipeline
            except Exception as e:
                logger.info("Pipeline exception: %s — %s", p_str, e)
        return None

    def _candidate_devices(self, preferred_device: str | None = None) -> list[str]:
        candidates: list[str] = []
        enumerated_devices = [camera["id"] for camera in enumerate_cameras()]

        def add(device: str | None):
            if device and device not in candidates:
                candidates.append(device)

        if IS_MACOS:
            add(preferred_device)
            add(self._current_device)
        else:
            if preferred_device in enumerated_devices:
                add(preferred_device)
            if self._current_device in enumerated_devices:
                add(self._current_device)

        for device_id in enumerated_devices:
            add(device_id)
        return candidates

    def _start_any_pipeline(self, preferred_device: str | None = None) -> tuple[Gst.Pipeline | None, str | None]:
        for device_id in self._candidate_devices(preferred_device):
            pipeline = self._start_pipeline(device_id)
            if pipeline:
                return pipeline, device_id
        return None, preferred_device or self._current_device

    def _clear_pipeline_locked(self):
        if self._bus is not None:
            if self._bus_watch_id is not None:
                try:
                    self._bus.disconnect(self._bus_watch_id)
                except Exception:
                    pass
                self._bus_watch_id = None
            try:
                self._bus.remove_signal_watch()
            except Exception:
                pass
            self._bus = None

        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    def _attach_pipeline_locked(self, pipeline: Gst.Pipeline, device_id: str | None):
        self.pipeline = pipeline
        self._current_device = device_id
        self._frame_count = 0

        sink = pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_new_sample)

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_watch_id = bus.connect("message", self._on_bus_message)
        self._bus = bus

        pipeline.set_state(Gst.State.PLAYING)
        logger.info("Camera streaming started on %s", device_id or "default device")

    def _ensure_restart_task(self, reason: str):
        if not self._loop:
            return
        if self._restart_task and not self._restart_task.done():
            return
        logger.info("Scheduling camera restart: %s", reason)
        self._restart_task = self._loop.create_task(self._restart_until_available(reason))

    def _request_restart(self, reason: str):
        if self._loop:
            self._loop.call_soon_threadsafe(self._ensure_restart_task, reason)

    def _on_bus_message(self, bus, message):
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.warning(
                "Camera pipeline error on %s: %s%s",
                self._current_device,
                err,
                f" ({debug})" if debug else "",
            )
        elif message.type == Gst.MessageType.EOS:
            logger.warning("Camera pipeline reached EOS on %s", self._current_device)
        else:
            return

        with self._lock:
            if bus != self._bus:
                return
            self._clear_pipeline_locked()

        self._request_restart("pipeline lost")

    async def _restart_until_available(self, reason: str):
        delay = 1.0
        try:
            while True:
                with self._lock:
                    if self.pipeline or not self.queues:
                        return
                    preferred_device = self._current_device

                pipeline, resolved_device = await asyncio.to_thread(
                    self._start_any_pipeline,
                    preferred_device,
                )

                if pipeline:
                    with self._lock:
                        if self.pipeline or not self.queues:
                            pipeline.set_state(Gst.State.NULL)
                            return
                        self._attach_pipeline_locked(pipeline, resolved_device)
                    return

                logger.info("Camera unavailable after %s; retrying in %.1fs", reason, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 5.0)
        finally:
            with self._lock:
                if self._restart_task is asyncio.current_task():
                    self._restart_task = None

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
        if not hasattr(self, "_frame_count"):
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
            self.queues[ws] = q
            should_restart = self.pipeline is None
        if should_restart:
            self._ensure_restart_task("client connected")
        logger.info("Client added (total: %d)", len(self.queues))
        return q

    def remove_client(self, ws: WebSocket):
        with self._lock:
            self.queues.pop(ws, None)
            if not self.queues:
                if self._restart_task and not self._restart_task.done():
                    self._restart_task.cancel()
                    self._restart_task = None
                self._clear_pipeline_locked()
                logger.info("Camera stopped (no clients)")
        logger.info("Client removed (total: %d)", len(self.queues))

    async def switch_camera(self, device_id: str):
        with self._lock:
            self._current_device = device_id
            self._clear_pipeline_locked()
        self._ensure_restart_task(f"switch to {device_id}")
        logger.info("Requested camera switch to %s", device_id)


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
    return JSONResponse(
        content={
            "mode": "mjpeg-ws",
            "cameras": cameras,
            "pipeline_state": camera.pipeline.get_state(0)[1].value_nick if camera.pipeline else None,
            "num_clients": len(camera.queues),
        }
    )


@app.get("/")
async def root():
    return FileResponse(Path(__file__).parent / "index.html", media_type="text/html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port={{.PORT}})
