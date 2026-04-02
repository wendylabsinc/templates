import asyncio
import glob
import json
import logging
import os
import platform
import subprocess
import threading
from pathlib import Path
from threading import Lock

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")

from gi.repository import Gst, GLib
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

Gst.init(None)
_glib_loop = GLib.MainLoop()
threading.Thread(target=_glib_loop.run, daemon=True).start()

app = FastAPI()

_static_dir = Path(__file__).parent / "static"

# ---------------------------------------------------------------------------
# Cars CRUD (SQLite — persisted at /data/cars.db)
# ---------------------------------------------------------------------------

import sqlite3

class CarInput(BaseModel):
    make: str
    model: str
    color: str
    year: int

_DB_PATH = Path("/data/cars.db")


def _get_db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            make TEXT NOT NULL,
            model TEXT NOT NULL,
            color TEXT NOT NULL,
            year INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn


@app.get("/api/cars")
def list_cars():
    db = _get_db()
    rows = db.execute("SELECT * FROM cars ORDER BY id").fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.post("/api/cars", status_code=201)
def create_car(car: CarInput):
    db = _get_db()
    cur = db.execute(
        "INSERT INTO cars (make, model, color, year) VALUES (?, ?, ?, ?)",
        (car.make, car.model, car.color, car.year),
    )
    db.commit()
    row = db.execute("SELECT * FROM cars WHERE id = ?", (cur.lastrowid,)).fetchone()
    db.close()
    return dict(row)


@app.get("/api/cars/{car_id}")
def get_car(car_id: int):
    db = _get_db()
    row = db.execute("SELECT * FROM cars WHERE id = ?", (car_id,)).fetchone()
    db.close()
    if not row:
        raise HTTPException(404, "Car not found")
    return dict(row)


@app.put("/api/cars/{car_id}")
def update_car(car_id: int, car: CarInput):
    db = _get_db()
    db.execute(
        "UPDATE cars SET make=?, model=?, color=?, year=? WHERE id=?",
        (car.make, car.model, car.color, car.year, car_id),
    )
    db.commit()
    row = db.execute("SELECT * FROM cars WHERE id = ?", (car_id,)).fetchone()
    db.close()
    if not row:
        raise HTTPException(404, "Car not found")
    return dict(row)


@app.delete("/api/cars/{car_id}", status_code=204)
def delete_car(car_id: int):
    db = _get_db()
    cur = db.execute("DELETE FROM cars WHERE id = ?", (car_id,))
    db.commit()
    db.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "Car not found")


# ---------------------------------------------------------------------------
# Camera — MJPEG over WebSocket
# ---------------------------------------------------------------------------

class MJPEGCamera:
    def __init__(self):
        self.pipeline = None
        self.queues: dict[WebSocket, asyncio.Queue] = {}
        self._lock = threading.Lock()

    def _start_pipeline(self):
        appsink = "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
        for desc in [
            f"v4l2src ! image/jpeg ! {appsink}",
            f"v4l2src ! image/jpeg,width=640,height=480 ! {appsink}",
            f"v4l2src ! videoconvert ! jpegenc quality=70 ! {appsink}",
        ]:
            try:
                p = Gst.parse_launch(desc)
                ret = p.set_state(Gst.State.PAUSED)
                if ret == Gst.StateChangeReturn.FAILURE:
                    p.set_state(Gst.State.NULL); continue
                if ret == Gst.StateChangeReturn.ASYNC:
                    r, _, _ = p.get_state(5 * Gst.SECOND)
                    if r == Gst.StateChangeReturn.FAILURE:
                        p.set_state(Gst.State.NULL); continue
                return p
            except Exception:
                continue
        return None

    def _on_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if not sample: return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok: return Gst.FlowReturn.OK
        data = bytes(mi.data)
        buf.unmap(mi)
        with self._lock:
            for q in self.queues.values():
                try: q.put_nowait(data)
                except asyncio.QueueFull: pass
        return Gst.FlowReturn.OK

    async def add_client(self, ws):
        q = asyncio.Queue(maxsize=2)
        with self._lock:
            if not self.pipeline:
                self.pipeline = self._start_pipeline()
                if not self.pipeline: raise RuntimeError("No camera")
                self.pipeline.get_by_name("sink").connect("new-sample", self._on_new_sample)
                self.pipeline.set_state(Gst.State.PLAYING)
            self.queues[ws] = q
        return q

    def remove_client(self, ws):
        with self._lock:
            self.queues.pop(ws, None)
            if not self.queues and self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None

camera = MJPEGCamera()


@app.websocket("/api/camera/stream")
async def camera_stream(ws: WebSocket):
    await ws.accept()
    try:
        q = await camera.add_client(ws)
    except Exception:
        await ws.close(1011); return
    try:
        while True:
            await ws.send_bytes(await q.get())
    except Exception:
        pass
    finally:
        camera.remove_client(ws)


# ---------------------------------------------------------------------------
# Audio — PCM S16LE over WebSocket
# ---------------------------------------------------------------------------

def _list_alsa_devices(cmd: str) -> list[dict]:
    devs = []
    try:
        out = subprocess.check_output(cmd.split(), stderr=subprocess.DEVNULL, timeout=2).decode()
        for line in out.splitlines():
            if line.startswith("card "):
                parts = line.split(":")
                if len(parts) >= 2:
                    card = line.split()[1].rstrip(":")
                    name = parts[1].strip().split("[")[0].strip()
                    devs.append({"id": f"hw:{card},0", "name": name})
    except Exception:
        pass
    return devs


class AudioCapture:
    def __init__(self):
        self.pipeline = None
        self.queues: dict[WebSocket, asyncio.Queue] = {}
        self._lock = threading.Lock()

    def _start_pipeline(self):
        appsink = "appsink name=sink emit-signals=true max-buffers=4 drop=true sync=false"
        pcm = "audio/x-raw,format=S16LE,channels=1,rate=16000"
        pipelines = []
        for mic in _list_alsa_devices("arecord -l"):
            pipelines.append(f'alsasrc device="{mic["id"]}" ! audioconvert ! audioresample ! {pcm} ! {appsink}')
        pipelines.append(f"alsasrc ! audioconvert ! audioresample ! {pcm} ! {appsink}")

        for desc in pipelines:
            try:
                p = Gst.parse_launch(desc)
                ret = p.set_state(Gst.State.PAUSED)
                if ret == Gst.StateChangeReturn.FAILURE:
                    p.set_state(Gst.State.NULL); continue
                if ret == Gst.StateChangeReturn.ASYNC:
                    r, _, _ = p.get_state(5 * Gst.SECOND)
                    if r == Gst.StateChangeReturn.FAILURE:
                        p.set_state(Gst.State.NULL); continue
                logger.info("Audio pipeline ready: %s", desc)
                return p
            except Exception:
                continue
        return None

    def _on_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if not sample: return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok: return Gst.FlowReturn.OK
        data = bytes(mi.data)
        buf.unmap(mi)
        with self._lock:
            for q in self.queues.values():
                try: q.put_nowait(data)
                except asyncio.QueueFull: pass
        return Gst.FlowReturn.OK

    async def add_client(self, ws):
        q = asyncio.Queue(maxsize=4)
        with self._lock:
            if not self.pipeline:
                self.pipeline = self._start_pipeline()
                if not self.pipeline: raise RuntimeError("No microphone")
                self.pipeline.get_by_name("sink").connect("new-sample", self._on_new_sample)
                self.pipeline.set_state(Gst.State.PLAYING)
            self.queues[ws] = q
        return q

    def remove_client(self, ws):
        with self._lock:
            self.queues.pop(ws, None)
            if not self.queues and self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None

audio_capture = AudioCapture()


@app.websocket("/api/audio/stream")
async def audio_stream(ws: WebSocket):
    await ws.accept()
    try:
        q = await audio_capture.add_client(ws)
    except Exception:
        await ws.close(1011); return
    try:
        while True:
            await ws.send_bytes(await q.get())
    except Exception:
        pass
    finally:
        audio_capture.remove_client(ws)


# ---------------------------------------------------------------------------
# GPU info
# ---------------------------------------------------------------------------

@app.get("/api/gpu")
def gpu_info():
    info = {"available": False}
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version,temperature.gpu",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=5
        ).decode().strip()
        if out:
            parts = [p.strip() for p in out.split(",")]
            info = {
                "available": True,
                "name": parts[0] if len(parts) > 0 else None,
                "memory": f"{parts[1]} MiB" if len(parts) > 1 else None,
                "driver": parts[2] if len(parts) > 2 else None,
                "temperature": f"{parts[3]}°C" if len(parts) > 3 else None,
            }
    except Exception:
        # Try Jetson tegrastats or /sys thermal
        try:
            temp = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
            info = {"available": True, "name": "ARM GPU", "temperature": f"{int(temp) / 1000:.1f}°C"}
        except Exception:
            pass
    return info


# ---------------------------------------------------------------------------
# System info
# ---------------------------------------------------------------------------

@app.get("/api/system")
def system_info():
    import shutil
    hostname = os.environ.get("WENDY_HOSTNAME", platform.node())
    mem = {}
    try:
        mi = Path("/proc/meminfo").read_text()
        for line in mi.splitlines():
            if line.startswith("MemTotal"):
                mem["total"] = f"{int(line.split()[1]) // 1024} MB"
            elif line.startswith("MemAvailable"):
                mem["free"] = f"{int(line.split()[1]) // 1024} MB"
        if "total" in mem and "free" in mem:
            total = int(mem["total"].split()[0])
            free = int(mem["free"].split()[0])
            mem["used"] = f"{total - free} MB"
    except Exception:
        pass

    disk = {}
    try:
        usage = shutil.disk_usage("/")
        disk = {
            "total": f"{usage.total // (1024**3)} GB",
            "used": f"{usage.used // (1024**3)} GB",
            "free": f"{usage.free // (1024**3)} GB",
        }
    except Exception:
        pass

    cpu = {}
    try:
        ci = Path("/proc/cpuinfo").read_text()
        models = [l.split(":")[1].strip() for l in ci.splitlines() if l.startswith("model name")]
        cpu = {"model": models[0] if models else platform.processor(), "cores": os.cpu_count() or 0}
    except Exception:
        cpu = {"model": platform.processor(), "cores": os.cpu_count() or 0}

    uptime = ""
    try:
        secs = float(Path("/proc/uptime").read_text().split()[0])
        h, m = int(secs // 3600), int((secs % 3600) // 60)
        uptime = f"{h}h {m}m"
    except Exception:
        pass

    return {
        "hostname": hostname,
        "platform": platform.system(),
        "architecture": platform.machine(),
        "uptime": uptime,
        "memory": mem,
        "disk": disk,
        "cpu": cpu,
    }


# ---------------------------------------------------------------------------
# Serve React SPA — MUST be last (catch-all)
# ---------------------------------------------------------------------------

@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    """Serve static files, fall back to index.html for SPA routing."""
    file_path = _static_dir / full_path
    if file_path.is_file():
        return FileResponse(file_path)
    return FileResponse(_static_dir / "index.html")
