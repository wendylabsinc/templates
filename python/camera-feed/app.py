import asyncio
import glob
import json
import subprocess
import threading
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("GLib", "2.0")

from gi.repository import GLib, Gst, GstApp  # noqa: E402
from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

Gst.init(None)

app = FastAPI()

_app_dir = Path(__file__).parent
_assets_dir = _app_dir / "assets"
if _assets_dir.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

# ---------------------------------------------------------------------------
# GLib main loop (runs in a daemon thread so GStreamer bus events are pumped)
# ---------------------------------------------------------------------------
_glib_loop = GLib.MainLoop()


def _run_glib_loop():
    _glib_loop.run()


_glib_thread = threading.Thread(target=_run_glib_loop, daemon=True)
_glib_thread.start()

# ---------------------------------------------------------------------------
# Camera helpers
# ---------------------------------------------------------------------------

PIPELINES = [
    'v4l2src device={device} ! image/jpeg,framerate=30/1 ! appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false',
    'v4l2src device={device} ! videoconvert ! jpegenc ! appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false',
]


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


def list_cameras() -> list[dict]:
    """Return available video capture devices."""
    # Try GstDeviceMonitor first.
    cameras: list[dict] = []
    try:
        monitor = Gst.DeviceMonitor.new()
        monitor.add_filter("Video/Source", None)
        monitor.start()
        for dev in monitor.get_devices():
            props = dev.get_properties()
            path = props.get_string("device.path") if props else None
            cameras.append({"id": path or "", "name": dev.get_display_name()})
        monitor.stop()
    except Exception:
        pass

    # Fallback: scan /dev/video* (works in containers without udev).
    if not cameras:
        for path in sorted(glob.glob("/dev/video*")):
            if _v4l2_is_capture(path):
                cameras.append({"id": path, "name": _v4l2_device_name(path)})

    return cameras


class CameraStream:
    """Shared singleton that captures MJPEG frames from a camera.

    Multiple WebSocket clients share one GStreamer pipeline. Each client
    gets its own asyncio.Queue; frames are broadcast to all queues.
    The pipeline starts on first client and stops when the last disconnects.
    """

    def __init__(self):
        self.device = "/dev/video0"
        self.pipeline = None
        self.appsink = None
        self._lock = threading.Lock()
        self._queues: dict[int, asyncio.Queue] = {}
        self._client_id = 0
        self._loop: asyncio.AbstractEventLoop | None = None

    def _start_pipeline(self):
        for tmpl in PIPELINES:
            desc = tmpl.format(device=self.device)
            try:
                pipeline = Gst.parse_launch(desc)
            except GLib.Error:
                continue
            sink = pipeline.get_by_name("sink")
            sink.connect("new-sample", self._on_new_sample)
            pipeline.set_state(Gst.State.PLAYING)
            ret = pipeline.get_state(2 * Gst.SECOND)
            if ret[0] == Gst.StateChangeReturn.FAILURE:
                pipeline.set_state(Gst.State.NULL)
                continue
            self.pipeline = pipeline
            self.appsink = sink
            return
        raise RuntimeError(
            f"Could not open camera {self.device} with any known pipeline"
        )

    def _on_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        data = bytes(mapinfo.data)
        buf.unmap(mapinfo)
        with self._lock:
            for q in self._queues.values():
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    pass
        return Gst.FlowReturn.OK

    def add_client(self) -> tuple[int, asyncio.Queue]:
        self._loop = asyncio.get_event_loop()
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2)
        with self._lock:
            self._client_id += 1
            cid = self._client_id
            self._queues[cid] = q
            if self.pipeline is None:
                self._start_pipeline()
        return cid, q

    def remove_client(self, cid: int):
        with self._lock:
            self._queues.pop(cid, None)
            if not self._queues and self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None
                self.appsink = None

    def switch(self, device: str):
        with self._lock:
            if self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None
                self.appsink = None
            self.device = device
            if self._queues:
                self._start_pipeline()


camera = CameraStream()


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).with_name("index.html")
    return HTMLResponse(content=html_path.read_text(), status_code=200)


@app.get("/cameras")
async def cameras():
    return list_cameras()


# ---------------------------------------------------------------------------
# WebSocket streaming
# ---------------------------------------------------------------------------


@app.websocket("/stream")
async def stream(ws: WebSocket):
    await ws.accept()

    try:
        cid, queue = camera.add_client()
    except RuntimeError as exc:
        await ws.close(code=1011, reason=str(exc))
        return

    async def _consumer():
        try:
            while True:
                frame = await queue.get()
                await ws.send_bytes(frame)
        except WebSocketDisconnect:
            pass

    async def _receiver():
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "switch_camera" in msg:
                    camera.switch(msg["switch_camera"])
        except WebSocketDisconnect:
            pass

    consumer = asyncio.create_task(_consumer())
    receiver = asyncio.create_task(_receiver())

    try:
        await asyncio.wait(
            [consumer, receiver],
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        consumer.cancel()
        receiver.cancel()
        camera.remove_client(cid)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    print("Starting camera-feed on port {{.PORT}}")
    uvicorn.run(app, host="0.0.0.0", port={{.PORT}})
