#!/usr/bin/env python3
"""
Audio streaming server.
GStreamer audio capture over WebSocket — captures raw PCM S16LE from the
microphone and broadcasts it to connected WebSocket clients. Can also
play .wav files on a connected speaker via GStreamer.
"""
import asyncio
import collections
import glob
import json
import logging
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

# Current speaker device for playback (set via /speakers endpoint or WS command).
_current_speaker: str | None = None


# ---------------------------------------------------------------------------
# Device enumeration
# ---------------------------------------------------------------------------


def _parse_arecord_or_aplay(cmd: str) -> list[dict]:
    """Parse `arecord -l` or `aplay -l` output into [{id, name}, ...]."""
    devices: list[dict] = []
    try:
        out = subprocess.check_output(cmd.split(), stderr=subprocess.DEVNULL, timeout=2).decode()
        for line in out.splitlines():
            if line.startswith("card "):
                parts = line.split(":")
                if len(parts) >= 2:
                    card_num = line.split()[1].rstrip(":")
                    name = parts[1].strip().split("[")[0].strip()
                    devices.append({"id": f"hw:{card_num},0", "name": name})
    except Exception:
        pass
    return devices


def _list_microphones() -> list[dict]:
    """Return available audio input (capture) devices."""
    return _parse_arecord_or_aplay("arecord -l")


def _list_speakers() -> list[dict]:
    """Return available audio output (playback) devices."""
    return _parse_arecord_or_aplay("aplay -l")


def _list_sounds() -> list[dict]:
    """Return .wav files in ./assets as [{name, file}, ...]."""
    sounds = []
    for f in sorted(_assets_dir.glob("*.wav")):
        display = f.stem.replace("-", " ").replace("_", " ").title()
        sounds.append({"name": display, "file": f.name})
    return sounds


# ---------------------------------------------------------------------------
# Audio capture singleton
# ---------------------------------------------------------------------------


class AudioCapture:
    """Captures raw PCM audio from a microphone using GStreamer appsink.

    Uses alsasrc with a specific hw device. Audio is resampled to S16LE
    mono 16kHz for the waveform visualization.
    """

    def __init__(self):
        self.pipeline = None
        self.queues: dict[WebSocket, asyncio.Queue] = {}
        self._lock = threading.Lock()
        self._current_device: str | None = None

    def _start_pipeline(self) -> Gst.Pipeline | None:
        appsink = "appsink name=sink emit-signals=true max-buffers=4 drop=true sync=false"
        pcm_caps = "audio/x-raw,format=S16LE,channels=1,rate=16000"

        pipelines = []

        if self._current_device:
            # User selected a specific device.
            pipelines.append(
                f'alsasrc device="{self._current_device}" ! audioconvert ! audioresample ! {pcm_caps} ! {appsink}'
            )
        else:
            # Try each known capture device.
            for mic in _list_microphones():
                dev = mic["id"]
                pipelines.append(
                    f'alsasrc device="{dev}" ! audioconvert ! audioresample ! {pcm_caps} ! {appsink}'
                )
            # Fallback generic.
            pipelines.append(f"alsasrc ! audioconvert ! audioresample ! {pcm_caps} ! {appsink}")

        for desc in pipelines:
            try:
                pipeline = Gst.parse_launch(desc)
                ret = pipeline.set_state(Gst.State.PAUSED)
                if ret == Gst.StateChangeReturn.FAILURE:
                    pipeline.set_state(Gst.State.NULL)
                    logger.info("Pipeline failed: %s", desc)
                    continue
                if ret == Gst.StateChangeReturn.ASYNC:
                    ret, _, _ = pipeline.get_state(5 * Gst.SECOND)
                    if ret == Gst.StateChangeReturn.FAILURE:
                        pipeline.set_state(Gst.State.NULL)
                        logger.info("Pipeline preroll failed: %s", desc)
                        continue
                logger.info("Pipeline ready: %s", desc)
                return pipeline
            except Exception as e:
                logger.info("Pipeline exception: %s — %s", desc, e)
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
        if not hasattr(self, '_sample_count'):
            self._sample_count = 0
        self._sample_count += 1
        if self._sample_count <= 3 or self._sample_count % 200 == 0:
            logger.info("Sample %d: %d bytes, %d queues", self._sample_count, len(data), len(self.queues))

        with self._lock:
            for q in self.queues.values():
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    pass

        return Gst.FlowReturn.OK

    async def add_client(self, ws: WebSocket) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=4)
        with self._lock:
            if not self.pipeline:
                self.pipeline = self._start_pipeline()
                if not self.pipeline:
                    raise RuntimeError("Could not start audio pipeline")
                sink = self.pipeline.get_by_name("sink")
                sink.connect("new-sample", self._on_new_sample)
                self.pipeline.set_state(Gst.State.PLAYING)
                logger.info("Audio capture started")
            self.queues[ws] = q
        logger.info("Client added (total: %d)", len(self.queues))
        return q

    def remove_client(self, ws: WebSocket):
        with self._lock:
            self.queues.pop(ws, None)
            if not self.queues and self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None
                logger.info("Audio capture stopped (no clients)")
        logger.info("Client removed (total: %d)", len(self.queues))

    async def switch_microphone(self, device_id: str):
        with self._lock:
            if self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None
                self._sample_count = 0
            self._current_device = device_id
            self.pipeline = self._start_pipeline()
            if not self.pipeline:
                raise RuntimeError(f"Could not start microphone {device_id}")
            sink = self.pipeline.get_by_name("sink")
            sink.connect("new-sample", self._on_new_sample)
            self.pipeline.set_state(Gst.State.PLAYING)
        logger.info("Switched to microphone %s", device_id)


audio = AudioCapture()


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


@app.get("/sounds")
async def list_sounds():
    return JSONResponse(content=_list_sounds())


@app.get("/microphones")
async def list_microphones():
    return JSONResponse(content=_list_microphones())


@app.get("/speakers")
async def list_speakers():
    return JSONResponse(content=_list_speakers())


@app.post("/play/{filename}")
async def play_sound(filename: str):
    """Play a wav file from ./assets on the device speaker via GStreamer."""
    global _current_speaker
    filepath = _assets_dir / filename
    if not filepath.exists() or not filename.endswith(".wav"):
        return JSONResponse(content={"error": "not found"}, status_code=404)

    if _current_speaker:
        sink = f'alsasink device="{_current_speaker}"'
    else:
        # Try to find a speaker automatically.
        speakers = _list_speakers()
        if speakers:
            sink = f'alsasink device="{speakers[0]["id"]}"'
        else:
            sink = "autoaudiosink"

    desc = f'filesrc location="{filepath}" ! wavparse ! audioconvert ! audioresample ! {sink}'
    try:
        pipeline = Gst.parse_launch(desc)
        pipeline.set_state(Gst.State.PLAYING)

        def _watch_bus():
            bus = pipeline.get_bus()
            msg = bus.timed_pop_filtered(
                30 * Gst.SECOND,
                Gst.MessageType.EOS | Gst.MessageType.ERROR,
            )
            if msg and msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                logger.error("Playback error: %s (%s)", err.message, debug)
            pipeline.set_state(Gst.State.NULL)

        threading.Thread(target=_watch_bus, daemon=True).start()
        logger.info("Playing %s via %s", filename, sink)
        return JSONResponse(content={"status": "playing", "file": filename})
    except Exception as e:
        logger.error("Failed to play %s: %s", filename, e)
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.post("/speaker/{device_id:path}")
async def set_speaker(device_id: str):
    """Set the active speaker device for playback."""
    global _current_speaker
    _current_speaker = device_id
    logger.info("Speaker set to %s", device_id)
    return JSONResponse(content={"status": "ok", "speaker": device_id})


@app.websocket("/stream")
async def websocket_stream(websocket: WebSocket):
    await websocket.accept()
    try:
        q = await audio.add_client(websocket)
    except Exception as e:
        logger.error(f"Failed to start audio capture: {e}")
        await websocket.close(code=1011)
        return

    async def send_audio():
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
                if "switch_microphone" in msg:
                    try:
                        await audio.switch_microphone(msg["switch_microphone"])
                    except Exception as e:
                        logger.error(f"Microphone switch failed: {e}")
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    try:
        done, pending = await asyncio.wait(
            [asyncio.create_task(send_audio()), asyncio.create_task(recv_commands())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        audio.remove_client(websocket)


@app.get("/logs")
async def get_logs():
    return JSONResponse(content=list(_log_buffer))


@app.get("/debug")
async def debug_info():
    return JSONResponse(content={
        "mode": "pcm-s16le-ws",
        "pipeline_state": audio.pipeline.get_state(0)[1].value_nick if audio.pipeline else None,
        "num_clients": len(audio.queues),
        "microphones": _list_microphones(),
        "speakers": _list_speakers(),
        "sounds": _list_sounds(),
    })


@app.get("/")
async def root():
    return FileResponse(Path(__file__).parent / "index.html", media_type="text/html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port={{.PORT}})
