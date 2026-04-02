"""Shared GStreamer appsink singleton pattern for camera/audio capture."""

import asyncio
import logging
import threading

from gi.repository import Gst
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class GstCaptureSink:
    """Base class for GStreamer appsink-based capture singletons.

    Subclasses provide `_build_pipelines()` which returns a list of
    pipeline description strings to try. The first one that prerolls
    successfully is used.
    """

    def __init__(self, max_queue: int = 2):
        self.pipeline = None
        self.queues: dict[WebSocket, asyncio.Queue] = {}
        self._lock = threading.Lock()
        self._current_device: str | None = None
        self._max_queue = max_queue

    def _build_pipelines(self) -> list[str]:
        raise NotImplementedError

    def _start_pipeline(self) -> Gst.Pipeline | None:
        for desc in self._build_pipelines():
            try:
                p = Gst.parse_launch(desc)
                ret = p.set_state(Gst.State.PAUSED)
                if ret == Gst.StateChangeReturn.FAILURE:
                    p.set_state(Gst.State.NULL)
                    continue
                if ret == Gst.StateChangeReturn.ASYNC:
                    r, _, _ = p.get_state(5 * Gst.SECOND)
                    if r == Gst.StateChangeReturn.FAILURE:
                        p.set_state(Gst.State.NULL)
                        continue
                logger.info("Pipeline ready: %s", desc)
                return p
            except Exception:
                continue
        return None

    def _on_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if not sample:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        data = bytes(mi.data)
        buf.unmap(mi)
        with self._lock:
            for q in self.queues.values():
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    pass
        return Gst.FlowReturn.OK

    async def add_client(self, ws: WebSocket) -> asyncio.Queue:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=self._max_queue)
        with self._lock:
            if not self.pipeline:
                self.pipeline = self._start_pipeline()
                if not self.pipeline:
                    raise RuntimeError("No device available")
                self.pipeline.get_by_name("sink").connect(
                    "new-sample", self._on_new_sample
                )
                self.pipeline.set_state(Gst.State.PLAYING)
            self.queues[ws] = q
        return q

    def remove_client(self, ws: WebSocket):
        with self._lock:
            self.queues.pop(ws, None)
            if not self.queues and self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None

    def switch_device(self, device: str):
        with self._lock:
            if self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None
            self._current_device = device
            if self.queues:
                self.pipeline = self._start_pipeline()
                if self.pipeline:
                    self.pipeline.get_by_name("sink").connect(
                        "new-sample", self._on_new_sample
                    )
                    self.pipeline.set_state(Gst.State.PLAYING)
        logger.info("Switched to %s", device)
