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

# GLib main loop — pumps GStreamer bus events including appsink signals.
_glib_loop = GLib.MainLoop()
threading.Thread(target=_glib_loop.run, daemon=True).start()

# ---------------------------------------------------------------------------
# Camera helpers
# ---------------------------------------------------------------------------


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

    if not cameras:
        for path in sorted(glob.glob("/dev/video*")):
            if _v4l2_is_capture(path):
                cameras.append({"id": path, "name": _v4l2_device_name(path)})

    return cameras


def _build_source(device: str) -> str:
    return f"v4l2src device={device}"


class MJPEGCamera:
    """Shared camera singleton. Uses the emit-signals callback model
    (same as the proven 30fps wendy sample) with multiple client queues.
    """

    def __init__(self):
        self.pipeline = None
        self.queues: dict[int, asyncio.Queue] = {}
        self._lock = threading.Lock()
        self._current_device: str = "/dev/video0"
        self._client_id = 0

    def _start_pipeline(self) -> Gst.Pipeline | None:
        src = _build_source(self._current_device)
        appsink = "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
        pipelines = [
            f"{src} ! image/jpeg ! {appsink}",
            f"{src} ! image/jpeg,width=640,height=480 ! {appsink}",
            f"{src} ! videoconvert ! jpegenc quality=70 ! {appsink}",
        ]

        for desc in pipelines:
            try:
                pipeline = Gst.parse_launch(desc)
                ret = pipeline.set_state(Gst.State.PAUSED)
                if ret == Gst.StateChangeReturn.FAILURE:
                    pipeline.set_state(Gst.State.NULL)
                    continue
                if ret == Gst.StateChangeReturn.ASYNC:
                    ret, _, _ = pipeline.get_state(5 * Gst.SECOND)
                    if ret == Gst.StateChangeReturn.FAILURE:
                        pipeline.set_state(Gst.State.NULL)
                        continue
                return pipeline
            except Exception:
                continue
        return None

    def _on_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if not sample:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        data = bytes(mapinfo.data)
        buf.unmap(mapinfo)

        with self._lock:
            for q in self.queues.values():
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    pass

        return Gst.FlowReturn.OK

    def add_client(self) -> tuple[int, asyncio.Queue]:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2)
        with self._lock:
            if not self.pipeline:
                self.pipeline = self._start_pipeline()
                if not self.pipeline:
                    raise RuntimeError("Could not start camera pipeline")
                sink = self.pipeline.get_by_name("sink")
                sink.connect("new-sample", self._on_new_sample)
                self.pipeline.set_state(Gst.State.PLAYING)
            self._client_id += 1
            cid = self._client_id
            self.queues[cid] = q
        return cid, q

    def remove_client(self, cid: int):
        with self._lock:
            self.queues.pop(cid, None)
            if not self.queues and self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None

    def switch_camera(self, device: str):
        with self._lock:
            if self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None
            self._current_device = device
            if self.queues:
                self.pipeline = self._start_pipeline()
                if self.pipeline:
                    sink = self.pipeline.get_by_name("sink")
                    sink.connect("new-sample", self._on_new_sample)
                    self.pipeline.set_state(Gst.State.PLAYING)


camera = MJPEGCamera()


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).with_name("index.html")
    return HTMLResponse(content=html_path.read_text(), status_code=200)


@app.get("/cameras")
async def cameras_endpoint():
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

    async def _send_frames():
        try:
            while True:
                data = await queue.get()
                await ws.send_bytes(data)
        except Exception:
            pass

    async def _recv_commands():
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                if "switch_camera" in msg:
                    camera.switch_camera(msg["switch_camera"])
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    try:
        done, pending = await asyncio.wait(
            [asyncio.create_task(_send_frames()), asyncio.create_task(_recv_commands())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        camera.remove_client(cid)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    print("Starting camera-feed on port {{.PORT}}")
    uvicorn.run(app, host="0.0.0.0", port={{.PORT}})
