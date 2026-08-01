"""Supervised Go2 WebRTC camera capture and localhost JPEG forwarding."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from typing import Any

import cv2
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Response
from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("go2-foxglove-camera")

GO2_IP = os.environ.get("GO2_IP", "192.168.123.161")
BRIDGE_FRAME_URL = os.environ.get("BRIDGE_FRAME_URL", "http://127.0.0.1:8769/frame")
BRIDGE_STATUS_URL = os.environ.get("BRIDGE_STATUS_URL", "http://127.0.0.1:8769/status")
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8768"))
HEALTH_BIND_HOST = os.environ.get("HEALTH_BIND_HOST", "0.0.0.0")
MAX_FPS = float(os.environ.get("MAX_FPS", "15"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "80"))
TRACK_TIMEOUT_S = float(os.environ.get("TRACK_TIMEOUT_S", "15"))
FRAME_TIMEOUT_S = float(os.environ.get("FRAME_TIMEOUT_S", "5"))
CAMERA_MAX_AGE_S = float(os.environ.get("CAMERA_MAX_AGE_S", "2"))

if MAX_FPS <= 0 or not 1 <= JPEG_QUALITY <= 100:
    raise ValueError("MAX_FPS must be positive and JPEG_QUALITY must be 1..100")

_lock = threading.RLock()
_latest_jpeg: bytes | None = None
_state: dict[str, Any] = {
    "status": "starting",
    "connected": False,
    "frames": 0,
    "failures": 0,
    "restarts": 0,
    "last_frame_monotonic": 0.0,
    "last_frame_at_ns": 0,
    "last_error": None,
    "last_error_at_ns": 0,
    "session_id": None,
}


def _snapshot() -> dict[str, Any]:
    with _lock:
        result = dict(_state)
    last = float(result.pop("last_frame_monotonic"))
    age = None if last <= 0 else max(0.0, time.monotonic() - last)
    result["age_s"] = None if age is None else round(age, 3)
    result["ok"] = bool(
        result["connected"] and age is not None and age <= CAMERA_MAX_AGE_S
    )
    result["go2_ip"] = GO2_IP
    return result


def _set_error(error: Exception) -> None:
    rendered = f"{type(error).__name__}: {error}"
    with _lock:
        _state["status"] = "retrying"
        _state["connected"] = False
        _state["failures"] += 1
        _state["last_error"] = rendered
        _state["last_error_at_ns"] = time.time_ns()
    logger.warning("camera session failed: %s", rendered)


api = FastAPI(title="Go2 WebRTC camera health")


@api.get("/healthz")
def healthz() -> dict[str, Any]:
    return _snapshot()


@api.get("/snapshot.jpg")
def snapshot() -> Response:
    """Return the most recently forwarded frame for commissioning checks."""

    with _lock:
        jpeg = _latest_jpeg
        age = (
            None
            if float(_state["last_frame_monotonic"]) <= 0
            else time.monotonic() - float(_state["last_frame_monotonic"])
        )
    if jpeg is None or age is None or age > CAMERA_MAX_AGE_S:
        raise HTTPException(503, "no fresh camera frame is available")
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


def _run_health_server() -> None:
    uvicorn.run(api, host=HEALTH_BIND_HOST, port=HEALTH_PORT, log_level="warning")


async def _forward_status(client: httpx.AsyncClient) -> None:
    while True:
        try:
            await client.post(BRIDGE_STATUS_URL, json=_snapshot(), timeout=1.0)
        except httpx.HTTPError as error:  # bridge can restart independently
            logger.debug("bridge status update failed: %s", error)
        await asyncio.sleep(1.0)


async def _run_session(client: httpx.AsyncClient) -> None:
    session_id = uuid.uuid4().hex
    tracks: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)

    async def offer_track(track: Any) -> None:
        if tracks.empty():
            tracks.put_nowait(track)

    connection = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=GO2_IP)
    try:
        with _lock:
            _state["status"] = "connecting"
            _state["restarts"] += 1
            _state["session_id"] = session_id
        await asyncio.wait_for(connection.connect(), timeout=TRACK_TIMEOUT_S)
        connection.video.add_track_callback(offer_track)
        connection.video.switchVideoChannel(True)
        track = await asyncio.wait_for(tracks.get(), timeout=TRACK_TIMEOUT_S)
        with _lock:
            _state["status"] = "connected"
            _state["connected"] = True
        logger.info("WebRTC video connected to %s", GO2_IP)

        frame_number = 0
        minimum_period = 1.0 / MAX_FPS
        last_forwarded = 0.0
        while True:
            frame = await asyncio.wait_for(track.recv(), timeout=FRAME_TIMEOUT_S)
            now = time.monotonic()
            if now - last_forwarded < minimum_period:
                continue
            image = frame.to_ndarray(format="bgr24")
            encoded, jpeg = cv2.imencode(
                ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
            )
            if not encoded:
                raise RuntimeError("OpenCV failed to encode the camera frame")
            captured_at_ns = time.time_ns()
            response = await client.post(
                BRIDGE_FRAME_URL,
                content=jpeg.tobytes(),
                headers={
                    "Content-Type": "image/jpeg",
                    "X-Camera-Session": session_id,
                    "X-Captured-At-Ns": str(captured_at_ns),
                    "X-Frame-Number": str(frame_number),
                },
                timeout=2.0,
            )
            response.raise_for_status()
            last_forwarded = now
            frame_number += 1
            with _lock:
                global _latest_jpeg
                _latest_jpeg = jpeg.tobytes()
                _state["status"] = "streaming"
                _state["connected"] = True
                _state["frames"] += 1
                _state["last_frame_monotonic"] = time.monotonic()
                _state["last_frame_at_ns"] = captured_at_ns
                _state["last_error"] = None
                _state["last_error_at_ns"] = 0
    finally:
        with _lock:
            _state["connected"] = False
        try:
            await connection.disconnect()
        except Exception as error:  # noqa: BLE001 - connection may already be gone
            logger.debug("WebRTC close failed: %s", error)


async def main() -> None:
    async with httpx.AsyncClient() as client:
        status_task = asyncio.create_task(_forward_status(client))
        try:
            while True:
                try:
                    await _run_session(client)
                except Exception as error:  # noqa: BLE001 - supervised reconnect loop
                    _set_error(error)
                    await asyncio.sleep(3.0)
        finally:
            status_task.cancel()


if __name__ == "__main__":
    threading.Thread(target=_run_health_server, name="health", daemon=True).start()
    asyncio.run(main())
