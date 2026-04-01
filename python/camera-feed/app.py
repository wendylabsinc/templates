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

Gst.init(None)

app = FastAPI()

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
    'v4l2src device={device} ! image/jpeg,framerate=30/1 ! appsink name=sink emit-signals=true sync=false',
    'v4l2src device={device} ! videoconvert ! jpegenc ! appsink name=sink emit-signals=true sync=false',
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
    """Wraps a GStreamer pipeline that pulls MJPEG frames from a camera."""

    def __init__(self, device: str = "/dev/video0"):
        self.device = device
        self.pipeline = None
        self.appsink = None
        self._start()

    # -- lifecycle -----------------------------------------------------------

    def _start(self):
        for tmpl in PIPELINES:
            desc = tmpl.format(device=self.device)
            try:
                pipeline = Gst.parse_launch(desc)
            except GLib.Error:
                continue
            sink = pipeline.get_by_name("sink")
            pipeline.set_state(Gst.State.PLAYING)
            # Give the pipeline a moment to negotiate
            ret = pipeline.get_state(Gst.CLOCK_TIME_NONE if False else 2 * Gst.SECOND)
            if ret[0] == Gst.StateChangeReturn.FAILURE:
                pipeline.set_state(Gst.State.NULL)
                continue
            self.pipeline = pipeline
            self.appsink = sink
            return
        raise RuntimeError(
            f"Could not open camera {self.device} with any known pipeline"
        )

    def stop(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            self.appsink = None

    def switch(self, device: str):
        self.stop()
        self.device = device
        self._start()

    # -- frame pull ----------------------------------------------------------

    def pull_frame(self) -> bytes | None:
        """Pull a single JPEG frame (blocking, short timeout)."""
        if self.appsink is None:
            return None
        sample = self.appsink.try_pull_sample(Gst.SECOND)
        if sample is None:
            return None
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        data = bytes(mapinfo.data)
        buf.unmap(mapinfo)
        return data


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

MAX_QUEUE = 2  # drop frames when the client can't keep up


@app.websocket("/stream")
async def stream(ws: WebSocket):
    await ws.accept()

    camera = CameraStream()
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MAX_QUEUE)
    stop_event = asyncio.Event()

    async def _producer():
        loop = asyncio.get_event_loop()
        while not stop_event.is_set():
            frame = await loop.run_in_executor(None, camera.pull_frame)
            if frame is None:
                await asyncio.sleep(0.01)
                continue
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                # drop oldest, enqueue newest
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(frame)
                except asyncio.QueueFull:
                    pass

    async def _consumer():
        try:
            while not stop_event.is_set():
                frame = await queue.get()
                await ws.send_bytes(frame)
        except WebSocketDisconnect:
            pass

    async def _receiver():
        """Listen for JSON control messages from the client."""
        try:
            while not stop_event.is_set():
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "switch_camera" in msg:
                    camera.switch(msg["switch_camera"])
        except WebSocketDisconnect:
            pass

    producer = asyncio.create_task(_producer())
    consumer = asyncio.create_task(_consumer())
    receiver = asyncio.create_task(_receiver())

    try:
        done, pending = await asyncio.wait(
            [producer, consumer, receiver],
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        stop_event.set()
        producer.cancel()
        consumer.cancel()
        receiver.cancel()
        camera.stop()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    print("Starting camera-feed on port {{.PORT}}")
    uvicorn.run(app, host="0.0.0.0", port={{.PORT}})
