"""FastAPI HTTP control plane for the Go2.

Brain-facing endpoint is `/velocity` (non-blocking, watchdog-protected).
`/move`, `/stop`, and the skill endpoints are useful for manual `curl`
testing.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from go2_controller import Go2Controller


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("go2-motion")


controller = Go2Controller()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    controller.connect()

    loop = asyncio.get_running_loop()

    def _stop_on_signal():
        logger.info("Signal received; stopping dog")
        asyncio.create_task(controller.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop_on_signal)
        except NotImplementedError:
            pass

    try:
        yield
    finally:
        try:
            await controller.stop()
        except Exception:
            logger.exception("Error stopping dog during shutdown")


app = FastAPI(title="go2-motion", lifespan=lifespan)


class VelocityBody(BaseModel):
    vx: float = Field(0.0, description="Forward velocity, m/s (clamped to ±0.6)")
    vy: float = Field(0.0, description="Strafe velocity, m/s (clamped to ±0.4)")
    vyaw: float = Field(0.0, description="Yaw rate, rad/s (clamped to ±1.0)")


class MoveBody(BaseModel):
    vx: float = Field(0.0, description="Forward velocity, m/s (clamped to ±0.6)")
    vy: float = Field(0.0, description="Strafe velocity, m/s (clamped to ±0.4)")
    vyaw: float = Field(0.0, description="Yaw rate, rad/s (clamped to ±1.0)")
    duration: float = Field(2.0, description="How long to move, seconds (0.1–10)")


@app.get("/health")
async def health():
    if controller._sport_client is None:
        return JSONResponse(
            {"ok": False, "reason": "sport_client_not_ready"}, status_code=503
        )
    return {"ok": True}


@app.get("/state")
async def state() -> dict:
    s = controller.latest_state()
    return {"ok": bool(s), "state": s}


@app.post("/velocity")
async def set_velocity(body: VelocityBody) -> dict:
    """Non-blocking velocity setter (brain-facing).

    Renews a 1 s watchdog. Brain hits this every tick to keep the dog
    moving; if the calls stop, the watchdog halts the dog automatically.
    """
    return {
        "result": await controller.set_velocity(
            vx=body.vx, vy=body.vy, vyaw=body.vyaw
        )
    }


@app.post("/move")
async def move(body: MoveBody) -> dict:
    """Blocking move with explicit duration. Useful for curl testing."""
    return {
        "result": await controller.move(
            vx=body.vx, vy=body.vy, vyaw=body.vyaw, duration=body.duration
        )
    }


@app.post("/stop")
async def stop() -> dict:
    return {"result": await controller.stop()}


@app.post("/stand")
async def stand() -> dict:
    return {"result": await controller.stand_up()}


@app.post("/sit")
async def sit() -> dict:
    return {"result": await controller.sit()}


@app.post("/lie")
async def lie() -> dict:
    return {"result": await controller.lie_down()}


@app.post("/hello")
async def hello() -> dict:
    return {"result": await controller.hello()}


@app.post("/dance")
async def dance() -> dict:
    return {"result": await controller.dance()}
