"""rc — minimal teleop UI for the G1.

Port of go2-rc's proxy. The motion API surface the motion service
exposes is a superset of go2-motion's (port :3201, /velocity, /move,
/stop, /sit, plus G1 posture + arm presets), so this proxy only adds
routes and changes env-var defaults; the sibling containers it targets
are this app's motion + camera services over host networking.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("g1-rc")


MOTION_URL = os.environ.get("G1_MOTION_URL", "http://127.0.0.1:3201").rstrip("/")
CAMERA_UPSTREAM_URL = os.environ.get(
    "CAMERA_UPSTREAM_URL", "http://127.0.0.1:8000/stream/color"
).strip()
PORT = int(os.environ.get("PORT", "3500"))
MOTION_TIMEOUT_S = float(os.environ.get("G1_MOTION_TIMEOUT", "5.0"))

STATIC_DIR = Path(__file__).parent / "static"


_motion_client: httpx.AsyncClient | None = None
_camera_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _motion_client, _camera_client
    _motion_client = httpx.AsyncClient(timeout=MOTION_TIMEOUT_S)
    _camera_client = httpx.AsyncClient(
        timeout=httpx.Timeout(None, connect=5.0)
    )
    logger.info(
        "g1-RC up. motion=%s camera=%s port=%d",
        MOTION_URL,
        CAMERA_UPSTREAM_URL or "(none)",
        PORT,
    )
    try:
        yield
    finally:
        if _motion_client is not None:
            await _motion_client.aclose()
        if _camera_client is not None:
            await _camera_client.aclose()


app = FastAPI(title="g1-RC", lifespan=lifespan)


class VelocityBody(BaseModel):
    vx: float = Field(0.0)
    vy: float = Field(0.0)
    vyaw: float = Field(0.0)


class MoveBody(BaseModel):
    vx: float = Field(0.0)
    vy: float = Field(0.0)
    vyaw: float = Field(0.0)
    duration: float = Field(1.0)


async def _motion_post(path: str, json: dict | None = None) -> dict:
    assert _motion_client is not None
    try:
        r = await _motion_client.post(f"{MOTION_URL}{path}", json=json)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"g1-motion unreachable: {exc}") from exc
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}


async def _motion_get(path: str) -> dict:
    assert _motion_client is not None
    try:
        r = await _motion_client.get(f"{MOTION_URL}{path}")
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"g1-motion unreachable: {exc}") from exc
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}


@app.get("/api/health")
async def health() -> dict:
    motion_ok = False
    motion_reason: str | None = None
    try:
        m = await _motion_get("/health")
        motion_ok = bool(m.get("ok", False))
        if not motion_ok:
            motion_reason = m.get("reason") or "not_ready"
    except HTTPException as exc:
        motion_reason = exc.detail
    return {
        "ok": True,
        "motion": {"ok": motion_ok, "reason": motion_reason, "url": MOTION_URL},
        "camera": {"url": CAMERA_UPSTREAM_URL or None},
    }


@app.get("/api/state")
async def state() -> dict:
    return await _motion_get("/state")


@app.post("/api/velocity")
async def velocity(body: VelocityBody) -> dict:
    return await _motion_post("/velocity", body.model_dump())


@app.post("/api/move")
async def move(body: MoveBody) -> dict:
    return await _motion_post("/move", body.model_dump())


@app.post("/api/stop")
async def stop() -> dict:
    return await _motion_post("/stop")


class StandBody(BaseModel):
    force: bool = Field(False)


@app.post("/api/stand")
async def stand(body: StandBody | None = None) -> dict:
    return await _motion_post("/stand", {"force": bool(body and body.force)})


@app.post("/api/running")
async def running() -> dict:
    return await _motion_post("/running")


@app.get("/api/locomotion")
async def locomotion() -> dict:
    return await _motion_get("/locomotion")


@app.post("/api/estop")
async def estop() -> dict:
    return await _motion_post("/estop")


@app.post("/api/estop/clear")
async def estop_clear() -> dict:
    return await _motion_post("/estop/clear")


@app.post("/api/sit")
async def sit() -> dict:
    return await _motion_post("/sit")


@app.post("/api/lie")
async def lie() -> dict:
    return await _motion_post("/lie")


@app.post("/api/hello")
async def hello() -> dict:
    return await _motion_post("/hello")


# G1-specific posture + gesture endpoints. g1-motion exposes these on
# the same port; on a Go2 they'd 404, which the UI tolerates.
@app.post("/api/balance")
async def balance() -> dict:
    return await _motion_post("/balance")


@app.post("/api/squat")
async def squat() -> dict:
    return await _motion_post("/squat")


@app.post("/api/damp")
async def damp() -> dict:
    return await _motion_post("/damp")


@app.post("/api/wave")
async def wave() -> dict:
    return await _motion_post("/wave")


@app.post("/api/shake")
async def shake() -> dict:
    return await _motion_post("/shake")


# Arm presets (arm_sdk via g1-motion). Each posts the equivalent
# /arm/<preset> on the motion service. The generic /api/arm forwards
# a JSON body for full preset + side + duration control.
class ArmBody(BaseModel):
    preset: str = Field("home")
    side: str = Field("both")
    duration: float = Field(2.0)
    release: bool = Field(False)


@app.post("/api/arm")
async def arm(body: ArmBody) -> dict:
    return await _motion_post("/arm", body.model_dump())


@app.post("/api/arm/raise_left")
async def arm_raise_left() -> dict:
    return await _motion_post("/arm/raise_left")


@app.post("/api/arm/raise_right")
async def arm_raise_right() -> dict:
    return await _motion_post("/arm/raise_right")


@app.post("/api/arm/hands_up")
async def arm_hands_up() -> dict:
    return await _motion_post("/arm/hands_up")


@app.post("/api/arm/point_forward")
async def arm_point_forward() -> dict:
    return await _motion_post("/arm/point_forward")


@app.post("/api/arm/salute")
async def arm_salute() -> dict:
    return await _motion_post("/arm/salute")


@app.post("/api/arm/home")
async def arm_home() -> dict:
    return await _motion_post("/arm/home")


@app.get("/api/camera")
async def camera() -> StreamingResponse:
    if not CAMERA_UPSTREAM_URL:
        raise HTTPException(503, "No camera configured (CAMERA_UPSTREAM_URL unset)")
    assert _camera_client is not None

    try:
        upstream = await _camera_client.send(
            _camera_client.build_request("GET", CAMERA_UPSTREAM_URL),
            stream=True,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"camera upstream unreachable: {exc}") from exc

    if upstream.status_code >= 400:
        body = await upstream.aread()
        await upstream.aclose()
        raise HTTPException(upstream.status_code, body.decode("utf-8", "replace"))

    media_type = upstream.headers.get(
        "content-type", "multipart/x-mixed-replace; boundary=frame"
    )

    async def relay() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(relay(), media_type=media_type)


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    target = STATIC_DIR / "index.html"
    if not target.is_file():
        raise HTTPException(500, f"index.html missing at {target}")
    return FileResponse(target, media_type="text/html")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
