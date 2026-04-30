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

# librealsense's named visual presets for the depth sensor. These can be
# applied live via set_option(visual_preset, ...) without restarting the
# pipeline. Keys match the slugs the frontend's preset combobox emits.
PRESET_MAP: dict[str, int] = {
    "default": int(rs.rs400_visual_preset.default),
    "hand": int(rs.rs400_visual_preset.hand),
    "high-accuracy": int(rs.rs400_visual_preset.high_accuracy),
    "high-density": int(rs.rs400_visual_preset.high_density),
    "medium-density": int(rs.rs400_visual_preset.medium_density),
}


class RealSensePump:
    """Owns the librealsense pipeline and publishes the latest JPEG per stream.

    A single background thread polls frames and re-encodes each stream to
    JPEG once per arrival; HTTP handlers just read the latest bytes.

    Reconfiguration is hot. Width/height/FPS changes restart the pump thread —
    existing MJPEG clients stay connected (we don't clear `_latest`, so the
    last frame keeps rendering) and pick up new frames as soon as the fresh
    pipeline produces them. Preset changes are applied live via
    `set_option(visual_preset, ...)` on the running depth sensor with no
    restart at all.
    """

    def __init__(self) -> None:
        self._colorizer = rs.colorizer()
        self._latest: dict[StreamId, bytes] = {}
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._width = 640
        self._height = 480
        self._fps = 30
        self._preset = "default"
        self._pending_preset: str | None = None
        self._jpeg_quality = 80
        self._client_count = 0
        self._lock = threading.Lock()
        # Per-stream FPS, sampled over a 1s window. Only the worker thread
        # writes these; readers (the /health endpoint) get a snapshot via
        # `get_fps`. Dict rebinds are atomic under the GIL, so no lock needed.
        self._fps_counts: dict[StreamId, int] = {sid: 0 for sid in STREAM_IDS}
        self._fps_window_start = time.monotonic()
        self._fps_latest: dict[StreamId, float] = {sid: 0.0 for sid in STREAM_IDS}

    def configure(self, width: int, height: int, fps: int, preset: str) -> None:
        with self._lock:
            wh_fps_changed = (width, height, fps) != (self._width, self._height, self._fps)
            preset_changed = preset != self._preset
            self._width, self._height, self._fps = width, height, fps
            self._preset = preset
            if preset_changed:
                self._pending_preset = preset
            if not (wh_fps_changed and self._thread is not None and self._client_count > 0):
                return
            # Snapshot the running thread and signal it to stop. We MUST drop
            # the lock before joining: the worker thread itself acquires
            # `self._lock` inside `_apply_pending_preset`, so holding the lock
            # across the join would deadlock.
            old_thread = self._thread
            self._thread = None
            self._stop.set()
        old_thread.join(timeout=2.0)
        with self._lock:
            # During the join, clients may have all disconnected, or
            # `add_client` may have already spawned a fresh thread. In either
            # case, don't start another one.
            if self._client_count == 0 or self._thread is not None:
                return
            self._stop.clear()
            self._pending_preset = self._preset
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def add_client(self) -> None:
        with self._lock:
            self._client_count += 1
            if self._client_count == 1 and self._thread is None:
                self._stop.clear()
                self._pending_preset = self._preset
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def remove_client(self) -> None:
        with self._lock:
            self._client_count = max(0, self._client_count - 1)
            if self._client_count == 0 and self._thread is not None:
                old_thread = self._thread
                self._thread = None
                self._stop.set()
            else:
                old_thread = None
        if old_thread:
            old_thread.join(timeout=2.0)
            with self._cond:
                self._latest.clear()

    def latest(self, stream_id: StreamId, timeout: float = 5.0) -> bytes | None:
        deadline = time.monotonic() + timeout
        with self._cond:
            while stream_id not in self._latest:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)
            return self._latest[stream_id]

    def _run(self) -> None:
        with self._lock:
            w, h, fps = self._width, self._height, self._fps

        pipeline = rs.pipeline()
        config = rs.config()
        config.enable_stream(rs.stream.color, w, h, rs.format.bgr8, fps)
        config.enable_stream(rs.stream.depth, w, h, rs.format.z16, fps)
        config.enable_stream(rs.stream.infrared, 1, w, h, rs.format.y8, fps)
        config.enable_stream(rs.stream.infrared, 2, w, h, rs.format.y8, fps)

        try:
            profile = pipeline.start(config)
        except RuntimeError as e:
            logger.error(
                "Failed to start RealSense pipeline at %dx%d @ %dfps: %s", w, h, fps, e
            )
            return

        depth_sensor: rs.sensor | None = None
        try:
            depth_sensor = profile.get_device().first_depth_sensor()
        except RuntimeError as e:
            logger.warning("No depth sensor on device, presets disabled: %s", e)

        logger.info("RealSense pipeline started (%dx%d @ %d fps)", w, h, fps)
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality]

        try:
            while not self._stop.is_set():
                self._apply_pending_preset(depth_sensor)
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
                    for sid in updates:
                        self._fps_counts[sid] += 1
                    now = time.monotonic()
                    elapsed = now - self._fps_window_start
                    if elapsed >= 1.0:
                        self._fps_latest = {
                            sid: round(count / elapsed, 1)
                            for sid, count in self._fps_counts.items()
                        }
                        self._fps_counts = {sid: 0 for sid in STREAM_IDS}
                        self._fps_window_start = now
        finally:
            try:
                pipeline.stop()
            except Exception:
                pass
            self._fps_latest = {sid: 0.0 for sid in STREAM_IDS}
            self._fps_counts = {sid: 0 for sid in STREAM_IDS}
            logger.info("RealSense pipeline stopped")

    def _apply_pending_preset(self, depth_sensor: rs.sensor | None) -> None:
        with self._lock:
            preset = self._pending_preset
            self._pending_preset = None
        if preset is None or depth_sensor is None:
            return
        value = PRESET_MAP.get(preset)
        if value is None:
            logger.warning("Unknown preset: %s", preset)
            return
        if not depth_sensor.supports(rs.option.visual_preset):
            return
        try:
            depth_sensor.set_option(rs.option.visual_preset, float(value))
            logger.info("Applied preset: %s", preset)
        except RuntimeError as e:
            logger.error("Failed to apply preset %s: %s", preset, e)


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
    preset: str = Query("default"),
) -> dict[str, object]:
    if preset not in PRESET_MAP:
        raise HTTPException(400, f"Unknown preset: {preset}. Valid: {sorted(PRESET_MAP)}")
    pump.configure(width, height, fps, preset)
    return {"width": width, "height": height, "fps": fps, "preset": preset}


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "streams": list(STREAM_IDS),
        "clients": pump._client_count,
        "fps": dict(pump._fps_latest),
    }


_dist = Path(os.environ.get("FRONTEND_DIST", "/app/dist"))
if _dist.is_dir():
    # Mount AFTER all API routes are registered so /stream/*, /config, /health
    # match first; everything else falls through to the SPA's index.html.
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
