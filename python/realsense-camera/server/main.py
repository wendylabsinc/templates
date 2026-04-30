"""MJPEG streaming server for Intel RealSense D415.

Exposes four independent MJPEG endpoints — color, left IR, right IR, and a
colorized depth view — so the React frontend can drop each into an <img>
tag without any custom decoding.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
import pyrealsense2 as rs
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("realsense")

StreamId = Literal["color", "ir-left", "ir-right", "depth"]
STREAM_IDS: tuple[StreamId, ...] = ("color", "ir-left", "ir-right", "depth")


class RealSensePump:
    """Owns the librealsense pipeline and publishes the latest JPEG per stream.

    A single background thread polls frames and re-encodes each stream to
    JPEG once per arrival; HTTP handlers just read the latest bytes.
    """

    def __init__(self) -> None:
        self._pipeline: rs.pipeline | None = None
        self._colorizer = rs.colorizer()
        self._latest: dict[StreamId, bytes] = {}
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._width = 640
        self._height = 480
        self._fps = 30
        self._jpeg_quality = 80
        self._client_count = 0
        self._lock = threading.Lock()

    def configure(self, width: int, height: int, fps: int) -> None:
        with self._lock:
            self._width, self._height, self._fps = width, height, fps

    def add_client(self) -> None:
        with self._lock:
            self._client_count += 1
            should_start = self._client_count == 1 and self._thread is None
        if should_start:
            self._start()

    def remove_client(self) -> None:
        with self._lock:
            self._client_count = max(0, self._client_count - 1)
            should_stop = self._client_count == 0
        if should_stop:
            self._stop_pipeline()

    def latest(self, stream_id: StreamId, timeout: float = 5.0) -> bytes | None:
        deadline = time.monotonic() + timeout
        with self._cond:
            while stream_id not in self._latest:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)
            return self._latest[stream_id]

    def _start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _stop_pipeline(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread:
            thread.join(timeout=2.0)
        with self._cond:
            self._latest.clear()

    def _run(self) -> None:
        pipeline = rs.pipeline()
        config = rs.config()
        w, h, fps = self._width, self._height, self._fps
        config.enable_stream(rs.stream.color, w, h, rs.format.bgr8, fps)
        config.enable_stream(rs.stream.depth, w, h, rs.format.z16, fps)
        config.enable_stream(rs.stream.infrared, 1, w, h, rs.format.y8, fps)
        config.enable_stream(rs.stream.infrared, 2, w, h, rs.format.y8, fps)

        try:
            pipeline.start(config)
        except RuntimeError as e:
            logger.error("Failed to start RealSense pipeline: %s", e)
            return

        self._pipeline = pipeline
        logger.info("RealSense pipeline started (%dx%d @ %d fps)", w, h, fps)
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality]

        try:
            while not self._stop.is_set():
                try:
                    frames = pipeline.wait_for_frames(timeout_ms=1000)
                except RuntimeError:
                    continue

                updates: dict[StreamId, bytes] = {}

                color = frames.get_color_frame()
                if color:
                    img = np.asanyarray(color.get_data())
                    ok, buf = cv2.imencode(".jpg", img, encode_params)
                    if ok:
                        updates["color"] = buf.tobytes()

                ir_left = frames.get_infrared_frame(1)
                if ir_left:
                    img = np.asanyarray(ir_left.get_data())
                    ok, buf = cv2.imencode(".jpg", img, encode_params)
                    if ok:
                        updates["ir-left"] = buf.tobytes()

                ir_right = frames.get_infrared_frame(2)
                if ir_right:
                    img = np.asanyarray(ir_right.get_data())
                    ok, buf = cv2.imencode(".jpg", img, encode_params)
                    if ok:
                        updates["ir-right"] = buf.tobytes()

                depth = frames.get_depth_frame()
                if depth:
                    colorized = self._colorizer.colorize(depth)
                    img = np.asanyarray(colorized.get_data())
                    ok, buf = cv2.imencode(".jpg", img, encode_params)
                    if ok:
                        updates["depth"] = buf.tobytes()

                if updates:
                    with self._cond:
                        self._latest.update(updates)
                        self._cond.notify_all()
        finally:
            try:
                pipeline.stop()
            except Exception:
                pass
            self._pipeline = None
            logger.info("RealSense pipeline stopped")


pump = RealSensePump()
app = FastAPI(title="RealSense MJPEG Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


BOUNDARY = "frame"


def _mjpeg_iter(stream_id: StreamId) -> Iterator[bytes]:
    pump.add_client()
    try:
        last: bytes | None = None
        while True:
            frame = pump.latest(stream_id)
            if frame is None:
                break
            if frame is last:
                time.sleep(0.005)
                continue
            last = frame
            yield (
                f"--{BOUNDARY}\r\n".encode()
                + b"Content-Type: image/jpeg\r\n"
                + f"Content-Length: {len(frame)}\r\n\r\n".encode()
                + frame
                + b"\r\n"
            )
    finally:
        pump.remove_client()


@app.get("/stream/{stream_id}")
def stream(stream_id: str) -> StreamingResponse:
    if stream_id not in STREAM_IDS:
        raise HTTPException(404, f"Unknown stream: {stream_id}")
    return StreamingResponse(
        _mjpeg_iter(stream_id),  # type: ignore[arg-type]
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
    )


@app.post("/config")
def configure(
    width: int = Query(640, ge=1),
    height: int = Query(480, ge=1),
    fps: int = Query(30, ge=1, le=300),
) -> dict[str, int]:
    pump.configure(width, height, fps)
    return {"width": width, "height": height, "fps": fps}


@app.get("/health")
def health() -> dict[str, object]:
    return {"streams": list(STREAM_IDS), "clients": pump._client_count}


_dist = Path(os.environ.get("FRONTEND_DIST", "/app/dist"))
if _dist.is_dir():
    # Mount AFTER all API routes are registered so /stream/*, /config, /health
    # match first; everything else falls through to the SPA's index.html.
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
