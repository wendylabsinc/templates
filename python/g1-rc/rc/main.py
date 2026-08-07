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
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("g1-rc")
logging.getLogger("httpx").setLevel(logging.WARNING)


MOTION_URL = os.environ.get("G1_MOTION_URL", "http://127.0.0.1:3201").rstrip("/")
CAMERA_UPSTREAM_URL = os.environ.get(
    "CAMERA_UPSTREAM_URL", "http://127.0.0.1:8000/stream/color"
).strip()
CAMERA_HEALTH_URL = os.environ.get(
    "CAMERA_HEALTH_URL", "http://127.0.0.1:8000/health"
).strip()
CAMERA_DIAGNOSTICS_URL = os.environ.get(
    "CAMERA_DIAGNOSTICS_URL", "http://127.0.0.1:8000/diagnostics"
).strip()
PORT = int(os.environ.get("PORT", "3500"))
MOTION_TIMEOUT_S = float(os.environ.get("G1_MOTION_TIMEOUT", "5.0"))
LOG_COMMAND = (
    "wendy device logs {{.APP_ID}} --device <g1-hostname> --tail 200"
)

STATIC_DIR = Path(__file__).parent / "web"


_motion_client: httpx.AsyncClient | None = None
_camera_client: httpx.AsyncClient | None = None


class RecentEventsHandler(logging.Handler):
    def __init__(self, limit: int = 80) -> None:
        super().__init__(logging.INFO)
        self.events: deque[dict[str, str]] = deque(maxlen=limit)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.events.append({
                "time": datetime.fromtimestamp(
                    record.created, timezone.utc
                ).isoformat(timespec="seconds"),
                "level": record.levelname.lower(),
                "service": "rc",
                "message": record.getMessage(),
            })
        except Exception:
            self.handleError(record)

    def snapshot(self) -> list[dict[str, str]]:
        return list(self.events)


recent_events = RecentEventsHandler()
logging.getLogger("g1-rc").addHandler(recent_events)
started_at = time.monotonic()
_last_upstream_log: dict[str, tuple[str, float]] = {}


def _log_upstream_failure(service: str, operation: str, detail: str) -> None:
    """Log immediately on change, then at most once every 15 seconds."""
    key = f"{service}:{operation}"
    previous, logged_at = _last_upstream_log.get(key, ("", 0.0))
    now = time.monotonic()
    if detail != previous or now - logged_at >= 15.0:
        logger.warning(
            "upstream_failed service=%s operation=%s detail=%s",
            service,
            operation,
            detail,
        )
        _last_upstream_log[key] = (detail, now)


async def _response_detail(response: httpx.Response) -> Any:
    try:
        payload = response.json()
    except ValueError:
        return response.text
    if isinstance(payload, dict) and "detail" in payload:
        return payload["detail"]
    return payload


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


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "request_failed method=%s path=%s error=%s: %s",
        request.method,
        request.url.path,
        type(exc).__name__,
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(
        {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": "Open Diagnostics and inspect the recent RC service events.",
        },
        status_code=500,
    )


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
        detail = f"g1-motion unreachable: {type(exc).__name__}: {exc}"
        _log_upstream_failure("motion", f"POST {path}", detail)
        raise HTTPException(
            502,
            detail={
                "code": "motion_unreachable",
                "message": detail,
                "hint": "Open Diagnostics and inspect the motion startup error.",
            },
        ) from exc
    if r.status_code >= 400:
        detail = await _response_detail(r)
        _log_upstream_failure("motion", f"POST {path}", str(detail))
        raise HTTPException(r.status_code, detail=detail)
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}


async def _motion_get(path: str) -> dict:
    assert _motion_client is not None
    try:
        r = await _motion_client.get(f"{MOTION_URL}{path}")
    except httpx.HTTPError as exc:
        detail = f"g1-motion unreachable: {type(exc).__name__}: {exc}"
        _log_upstream_failure("motion", f"GET {path}", detail)
        raise HTTPException(
            502,
            detail={
                "code": "motion_unreachable",
                "message": detail,
                "hint": "Open Diagnostics and inspect the motion startup error.",
            },
        ) from exc
    if r.status_code >= 400:
        detail = await _response_detail(r)
        _log_upstream_failure("motion", f"GET {path}", str(detail))
        raise HTTPException(r.status_code, detail=detail)
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}


async def _camera_get(url: str, operation: str) -> dict:
    assert _camera_client is not None
    try:
        response = await _camera_client.get(url, timeout=5.0)
    except httpx.HTTPError as exc:
        detail = f"g1-camera unreachable: {type(exc).__name__}: {exc}"
        _log_upstream_failure("camera", operation, detail)
        return {
            "ok": False,
            "status": "unreachable",
            "error": detail,
            "hint": "Check CAMERA_SOURCE and inspect the camera service logs.",
        }
    payload = await _response_detail(response)
    if not isinstance(payload, dict):
        payload = {"ok": False, "error": str(payload)}
    if response.status_code >= 400 or not payload.get("ok", False):
        _log_upstream_failure(
            "camera", operation, str(payload.get("error") or payload)
        )
    return payload


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
    camera = await _camera_get(CAMERA_HEALTH_URL, "GET /health")
    camera_ok = bool(camera.get("ok", False))
    return {
        "ok": motion_ok and camera_ok,
        "motion": {"ok": motion_ok, "reason": motion_reason, "url": MOTION_URL},
        "camera": {
            "ok": camera_ok,
            "status": camera.get("status"),
            "reason": camera.get("error") or camera.get("error_code"),
            "hint": camera.get("hint"),
            "url": CAMERA_UPSTREAM_URL or None,
        },
    }


@app.get("/api/diagnostics")
async def diagnostics() -> dict:
    try:
        motion = await _motion_get("/diagnostics")
    except HTTPException as exc:
        motion = {
            "service": "motion",
            "status": "unreachable",
            "ok": False,
            "error": exc.detail,
            "hint": "Inspect the motion container logs for its startup traceback.",
            "events": [],
        }
    camera = await _camera_get(CAMERA_DIAGNOSTICS_URL, "GET /diagnostics")
    camera.setdefault("service", "camera")
    return {
        "ok": bool(motion.get("ok")) and bool(camera.get("ok")),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "log_command": LOG_COMMAND,
        "services": [
            {
                "service": "rc",
                "status": "ready",
                "ok": True,
                "uptime_seconds": round(time.monotonic() - started_at, 1),
                "checks": {
                    "motion_url": MOTION_URL,
                    "camera_health_url": CAMERA_HEALTH_URL,
                },
                "events": recent_events.snapshot(),
            },
            motion,
            camera,
        ],
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
        detail = f"camera upstream unreachable: {type(exc).__name__}: {exc}"
        _log_upstream_failure("camera", "GET /stream/color", detail)
        raise HTTPException(
            502,
            detail={
                "code": "camera_unreachable",
                "message": detail,
                "hint": "Open Diagnostics and check CAMERA_SOURCE.",
            },
        ) from exc

    if upstream.status_code >= 400:
        body = await upstream.aread()
        await upstream.aclose()
        detail = body.decode("utf-8", "replace")
        _log_upstream_failure("camera", "GET /stream/color", detail)
        raise HTTPException(upstream.status_code, detail=detail)

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
