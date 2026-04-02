#!/usr/bin/env python3
"""
Audio streaming server.
GStreamer audio capture over WebSocket — captures raw PCM S16LE from the
microphone and broadcasts it to connected WebSocket clients.
"""
import asyncio
import collections
import json
import logging
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


class AudioCapture:
    """Captures raw PCM audio from a microphone using GStreamer appsink.

    Audio is captured as S16LE, mono, 16 kHz and broadcast as raw PCM
    bytes to all connected WebSocket clients.
    """

    def __init__(self):
        self.pipeline = None
        self.queues: dict[WebSocket, asyncio.Queue] = {}
        self._lock = threading.Lock()
        self._loop = None

    def _start_pipeline(self) -> Gst.Pipeline | None:
        appsink = "appsink name=sink emit-signals=true max-buffers=4 drop=true sync=false"
        pcm_caps = "audio/x-raw,format=S16LE,channels=1,rate=16000"
        pipelines = [
            f"autoaudiosrc ! audioconvert ! {pcm_caps} ! {appsink}",
            f"alsasrc ! audioconvert ! {pcm_caps} ! {appsink}",
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
        if not hasattr(self, '_sample_count'):
            self._sample_count = 0
        self._sample_count += 1
        if self._sample_count <= 3 or self._sample_count % 100 == 0:
            logger.info("Sample %d: %d bytes, %d queues", self._sample_count, len(data), len(self.queues))

        with self._lock:
            for q in self.queues.values():
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    pass

        return Gst.FlowReturn.OK

    async def add_client(self, ws: WebSocket) -> asyncio.Queue:
        self._loop = asyncio.get_running_loop()
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


audio = AudioCapture()


def _list_sounds() -> list[dict]:
    """Return .wav files in ./assets as [{name, file}, ...]."""
    sounds = []
    for f in sorted(_assets_dir.glob("*.wav")):
        display = f.stem.replace("-", " ").replace("_", " ").title()
        sounds.append({"name": display, "file": f.name})
    return sounds


@app.get("/sounds")
async def list_sounds():
    return JSONResponse(content=_list_sounds())


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
                if "play" in msg:
                    logger.info("Client requested playback: %s", msg["play"])
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
        "sounds": _list_sounds(),
    })


@app.get("/")
async def root():
    return FileResponse(Path(__file__).parent / "index.html", media_type="text/html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port={{.PORT}})
