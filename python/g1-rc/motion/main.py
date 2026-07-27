"""FastAPI HTTP control plane for the G1 humanoid.

Brain-facing endpoint is `/velocity` (non-blocking, watchdog-protected).
`/move`, `/stop`, and the skill endpoints are useful for manual `curl`
testing.

Mirrors `go2-motion/main.py`'s shape so go2-brain / go2-mcp / g1-RC all
drive a G1 with the same HTTP contract they use for the Go2. Only the
underlying SDK changes (LocoClient instead of SportClient).
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from g1_controller import G1Controller


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("g1-motion")


controller = G1Controller()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    controller.connect()

    loop = asyncio.get_running_loop()

    def _stop_on_signal():
        logger.info("Signal received; stopping G1")
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
            logger.exception("Error stopping G1 during shutdown")


app = FastAPI(title="g1-motion", lifespan=lifespan)


class VelocityBody(BaseModel):
    vx: float = Field(0.0, description="Forward velocity, m/s (clamped to ±0.6)")
    vy: float = Field(0.0, description="Strafe velocity, m/s (clamped to ±0.4)")
    vyaw: float = Field(0.0, description="Yaw rate, rad/s (clamped to ±1.0)")


class MoveBody(BaseModel):
    vx: float = Field(0.0)
    vy: float = Field(0.0)
    vyaw: float = Field(0.0)
    duration: float = Field(2.0, description="How long to move, seconds (0.1–10)")


class WaveBody(BaseModel):
    with_turn: bool = Field(False, description="Add a body turn while waving")


class ShakeBody(BaseModel):
    stage: int = Field(0, description="Handshake stage: 0=extend, 1=complete")


@app.get("/health")
async def health():
    if controller._loco_client is None:
        return JSONResponse(
            {"ok": False, "reason": "loco_client_not_ready"}, status_code=503
        )
    return {"ok": True}


@app.get("/state")
async def state() -> dict:
    s = controller.latest_state()
    return {"ok": bool(s), "state": s}


@app.post("/velocity")
async def set_velocity(body: VelocityBody) -> dict:
    return {
        "result": await controller.set_velocity(
            vx=body.vx, vy=body.vy, vyaw=body.vyaw
        )
    }


@app.post("/move")
async def move(body: MoveBody) -> dict:
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
    """Stand up from sit/lie into a static standing posture."""
    return {"result": await controller.stand_up()}


@app.post("/balance")
async def balance(balance_mode: int = 0) -> dict:
    """Active balance-stand — the 'ready to walk' pose.

    `balance_mode` query param: 0 = static (default, safest), 1 =
    dynamic. The G1's LocoClient requires this argument; some
    firmware versions silently accept invalid values.
    """
    return {"result": await controller.balance_stand(balance_mode)}


@app.post("/squat")
async def squat() -> dict:
    return {"result": await controller.squat()}


@app.post("/sit")
async def sit() -> dict:
    """Sit down on the floor. G1 doesn't have a chair-sit; this is
    the full SitDown posture, equivalent to `/lie` on go2-motion."""
    return {"result": await controller.sit_down()}


@app.post("/lie")
async def lie() -> dict:
    """Recover from a lying position back to standing.

    Named `/lie` for symmetry with go2-motion, but on G1 this is the
    *recovery* direction (Lie → Stand). LocoClient v1 has no
    'go lie down' command — use `/sit` to end up on the floor instead.
    """
    return {"result": await controller.lie_to_stand()}


@app.post("/damp")
async def damp() -> dict:
    """Damping mode — joints go compliant. Safest soft-stop."""
    return {"result": await controller.damp()}


# -- hand gestures (arms via LocoClient; NOT Dex3 fingers) --------------------

@app.post("/hello")
async def hello() -> dict:
    """Alias of /wave for compatibility with go2-motion's API."""
    return {"result": await controller.wave_hand(with_turn=False)}


@app.post("/wave")
async def wave(body: WaveBody | None = None) -> dict:
    return {
        "result": await controller.wave_hand(
            with_turn=bool(body and body.with_turn)
        )
    }


@app.post("/shake")
async def shake(body: ShakeBody | None = None) -> dict:
    return {
        "result": await controller.shake_hand(
            stage=int(body.stage) if body else 0
        )
    }


# -- preset arm poses (low-level arm_sdk) -------------------------------------
# Implementation in g1_arm.py. These take a couple of seconds to ramp;
# the brain-facing /velocity loop should not be active concurrently.

class ArmPresetBody(BaseModel):
    preset: str = Field("home", description="home / raise / point_forward / hands_up / salute")
    side: str = Field("both", description="left / right / both")
    duration: float = Field(2.0, description="ramp seconds, 0.5–8")
    release: bool = Field(False, description="Fade arm_sdk back to LocoClient after the move")


@app.post("/arm")
async def arm_pose(body: ArmPresetBody) -> dict:
    """Move arms to a named preset. See `g1_arm.PRESETS_LEFT` for the list."""
    duration = max(0.5, min(body.duration, 8.0))
    return {
        "result": await controller.arm_preset(
            preset=body.preset, side=body.side,
            duration=duration, release=body.release,
        )
    }


# Convenience aliases for the most common one-shots — saves the caller
# having to construct a JSON body. Each accepts a `side` query param
# (defaults below match the natural side of the gesture).
@app.post("/arm/raise_left")
async def arm_raise_left() -> dict:
    return {"result": await controller.arm_preset("raise", side="left")}


@app.post("/arm/raise_right")
async def arm_raise_right() -> dict:
    return {"result": await controller.arm_preset("raise", side="right")}


@app.post("/arm/hands_up")
async def arm_hands_up() -> dict:
    return {"result": await controller.arm_preset("hands_up", side="both")}


@app.post("/arm/point_forward")
async def arm_point_forward() -> dict:
    return {"result": await controller.arm_preset("point_forward", side="both")}


@app.post("/arm/salute")
async def arm_salute() -> dict:
    return {"result": await controller.arm_preset("salute", side="right")}


@app.post("/arm/home")
async def arm_home() -> dict:
    """Return arms to home (hanging at sides) and release control back
    to LocoClient so the next walk command isn't fought by arm_sdk."""
    return {
        "result": await controller.arm_preset(
            "home", side="both", duration=2.0, release=True,
        )
    }


# -- Dex3 finger control (stubbed in v1) --------------------------------------

@app.post("/grip")
async def grip() -> dict:
    raise HTTPException(
        503,
        "Dex3 finger control not implemented in v1. LocoClient gestures "
        "(/wave, /shake) work; per-finger pose control needs Dex3Client "
        "wired separately. See g1_controller.py for the TODO.",
    )


@app.post("/release")
async def release() -> dict:
    raise HTTPException(503, "Dex3 finger control not implemented in v1.")


# Compatibility shim: go2-motion exposes /dance. G1 doesn't have a
# built-in dance routine in LocoClient — return 503 so the existing
# UI button stays functional but degrades cleanly.
@app.post("/dance")
async def dance() -> dict:
    raise HTTPException(
        503,
        "G1 LocoClient has no built-in dance. Consider a custom sequence "
        "of Squat / BalanceStand / WaveHand if you want one.",
    )
