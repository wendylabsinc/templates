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
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from g1_controller import G1Controller


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("g1-motion")


class RecentEventsHandler(logging.Handler):
    """Bounded, UI-safe copy of this service's recent log events."""

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
                "service": "motion",
                "message": record.getMessage(),
            })
        except Exception:
            self.handleError(record)

    def snapshot(self) -> list[dict[str, str]]:
        return list(self.events)


recent_events = RecentEventsHandler()
logging.getLogger("g1-motion").addHandler(recent_events)

controller = G1Controller()
started_at = time.monotonic()
startup: dict[str, Any] = {
    "status": "starting",
    "error": None,
    "hint": "Waiting for the Unitree SDK and DDS connection.",
}


def _startup_hint(exc: Exception) -> str:
    message = str(exc).lower()
    if "192.168.123" in message or "network_interface" in message:
        return (
            "PC2 cannot see the G1 robot bus. Check that the robot is fully "
            "powered on and that NETWORK_INTERFACE names the interface with "
            "a 192.168.123.x address."
        )
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return (
            "The Unitree Python SDK did not load. Rebuild the motion image and "
            "inspect the first import error in the motion service logs."
        )
    return (
        "The Unitree motion connection did not start. Check the motion service "
        "logs for the first traceback and verify the G1 is reachable on its "
        "192.168.123.x robot network."
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    monitor_started = False
    startup.update(status="connecting", error=None)
    logger.info(
        "startup stage=connecting network_setting=%s",
        controller._network_interface_setting,
    )
    try:
        controller.connect()
        await controller.start_fsm_monitor()
        monitor_started = True
    except Exception as exc:
        startup.update(
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            hint=_startup_hint(exc),
        )
        logger.exception(
            "startup stage=failed error=%s hint=%s",
            startup["error"],
            startup["hint"],
        )
    else:
        startup.update(status="ready", error=None, hint=None)
        logger.info(
            "startup stage=ready network_interface=%s",
            controller._network_interface,
        )

    loop = asyncio.get_running_loop()

    def _stop_on_signal():
        logger.info("Signal received; stopping G1")
        if controller._loco_client is not None:
            asyncio.create_task(controller.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop_on_signal)
        except NotImplementedError:
            pass

    try:
        yield
    finally:
        if monitor_started:
            await controller.stop_fsm_monitor()
        if controller._loco_client is not None:
            try:
                await controller.stop()
            except Exception:
                logger.exception("Error stopping G1 during shutdown")


app = FastAPI(title="g1-motion", lifespan=lifespan)


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
            "hint": "Open Diagnostics in the G1 UI, then inspect the motion service logs.",
        },
        status_code=500,
    )


class VelocityBody(BaseModel):
    vx: float = Field(0.0, description="Forward velocity, m/s (clamped to ±0.6)")
    vy: float = Field(0.0, description="Strafe velocity, m/s (clamped to ±0.4)")
    vyaw: float = Field(0.0, description="Yaw rate, rad/s (clamped to ±1.0)")


class MoveBody(BaseModel):
    vx: float = Field(0.0)
    vy: float = Field(0.0)
    vyaw: float = Field(0.0)
    duration: float = Field(2.0, description="How long to move, seconds (0.1–10)")


class StandBody(BaseModel):
    force: bool = Field(
        False,
        description="Allow the DAMP transition from an unexpected FSM state "
                    "(collapses the robot unless crane-supported)",
    )


class WaveBody(BaseModel):
    with_turn: bool = Field(False, description="Add a body turn while waving")


class ShakeBody(BaseModel):
    stage: int = Field(0, description="Handshake stage: 0=extend, 1=complete")


def _require_not_estopped() -> None:
    """Gate for every motion-causing endpoint. /stop and /damp stay
    reachable while latched — they make the robot safer, not less."""
    _require_ready()
    if controller.estop_latched():
        raise HTTPException(
            409, "E-stop latched — POST /estop/clear first"
        )


def _require_ready() -> None:
    if startup["status"] != "ready" or controller._loco_client is None:
        raise HTTPException(
            503,
            detail={
                "code": "motion_not_ready",
                "message": startup["error"] or "Unitree motion is still starting.",
                "hint": startup["hint"],
            },
        )


@app.get("/health")
async def health():
    if startup["status"] != "ready" or controller._loco_client is None:
        return JSONResponse(
            {
                "ok": False,
                "reason": "motion_startup_failed"
                if startup["status"] == "failed"
                else "loco_client_not_ready",
                "error": startup["error"],
                "hint": startup["hint"],
            },
            status_code=503,
        )
    if controller.estop_latched():
        return {"ok": True, "estop_latched": True}
    return {"ok": True}


@app.get("/diagnostics")
async def diagnostics() -> dict:
    state = controller.latest_state()
    fsm = await controller.fsm_status()
    checks = {
        "sdk_connected": controller._loco_client is not None,
        "dds_interface": controller._network_interface,
        "lowstate_received": "tick" in state,
        "battery_received": "battery_soc" in state,
        "fsm_fresh": bool(fsm.get("fsm_fresh", False)),
    }
    return {
        "service": "motion",
        "status": startup["status"],
        "ok": startup["status"] == "ready" and checks["sdk_connected"],
        "uptime_seconds": round(time.monotonic() - started_at, 1),
        "error": startup["error"],
        "hint": startup["hint"],
        "checks": checks,
        "config": {
            "network_setting": controller._network_interface_setting,
            "network_interface": controller._network_interface,
            "lowstate_topic": "rt/lowstate",
            "battery_topic": "rt/lf/bmsstate",
        },
        "fsm": fsm,
        "events": recent_events.snapshot(),
    }


@app.get("/locomotion")
async def locomotion() -> dict:
    """FSM snapshot: fsm_id, standing, ready_to_walk, estop_latched."""
    return await controller.fsm_status()


@app.get("/state")
async def state() -> dict:
    s = controller.latest_state()
    return {"ok": bool(s), "state": s}


@app.post("/velocity")
async def set_velocity(body: VelocityBody) -> dict:
    _require_not_estopped()
    return {
        "result": await controller.set_velocity(
            vx=body.vx, vy=body.vy, vyaw=body.vyaw
        )
    }


@app.post("/move")
async def move(body: MoveBody) -> dict:
    _require_not_estopped()
    return {
        "result": await controller.move(
            vx=body.vx, vy=body.vy, vyaw=body.vyaw, duration=body.duration
        )
    }


@app.post("/stop")
async def stop() -> dict:
    _require_ready()
    return {"result": await controller.stop()}


@app.post("/stand")
async def stand(body: StandBody | None = None) -> dict:
    """Stand via the native FSM (StopMove → ai mode → DAMP → LOCK STAND).

    From an unexpected FSM state the sequence must pass through DAMP,
    which collapses an unsupported robot — that path is refused unless
    the body carries `force: true`.
    """
    _require_not_estopped()
    return {"result": await controller.stand_up(force=bool(body and body.force))}


@app.post("/running")
async def running() -> dict:
    """LOCK STAND → RUNNING: the walk-ready policy. Velocity commands
    only actuate in this state. Call /stand first."""
    _require_not_estopped()
    return {"result": await controller.enter_running()}


@app.post("/balance")
async def balance() -> dict:
    """Deprecated alias of /running. `BalanceStand()` was verified not
    to work on the firmware this template targets; 'ready to walk' is
    the RUNNING FSM policy."""
    _require_not_estopped()
    return {"result": await controller.enter_running()}


@app.post("/squat")
async def squat() -> dict:
    _require_not_estopped()
    return {"result": await controller.squat()}


@app.post("/sit")
async def sit() -> dict:
    _require_not_estopped()
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
    _require_not_estopped()
    return {"result": await controller.lie_to_stand()}


@app.post("/damp")
async def damp() -> dict:
    """Damping mode — joints go compliant. Safest soft-stop. NOT gated
    by the e-stop latch (it makes the robot safer, not less)."""
    _require_ready()
    return {"result": await controller.damp()}


@app.post("/estop")
async def estop() -> dict:
    """Latching soft e-stop: halt walking, release arms, refuse all
    motion until /estop/clear."""
    _require_ready()
    return {"result": await controller.estop()}


@app.post("/estop/clear")
async def estop_clear() -> dict:
    _require_ready()
    return {"result": await controller.clear_estop()}


# -- hand gestures (arms via LocoClient; NOT Dex3 fingers) --------------------

@app.post("/hello")
async def hello() -> dict:
    """Alias of /wave for compatibility with go2-motion's API."""
    _require_not_estopped()
    return {"result": await controller.wave_hand(with_turn=False)}


@app.post("/wave")
async def wave(body: WaveBody | None = None) -> dict:
    _require_not_estopped()
    return {
        "result": await controller.wave_hand(
            with_turn=bool(body and body.with_turn)
        )
    }


@app.post("/shake")
async def shake(body: ShakeBody | None = None) -> dict:
    _require_not_estopped()
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
    _require_not_estopped()
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
    _require_not_estopped()
    return {"result": await controller.arm_preset("raise", side="left")}


@app.post("/arm/raise_right")
async def arm_raise_right() -> dict:
    _require_not_estopped()
    return {"result": await controller.arm_preset("raise", side="right")}


@app.post("/arm/hands_up")
async def arm_hands_up() -> dict:
    _require_not_estopped()
    return {"result": await controller.arm_preset("hands_up", side="both")}


@app.post("/arm/point_forward")
async def arm_point_forward() -> dict:
    _require_not_estopped()
    return {"result": await controller.arm_preset("point_forward", side="both")}


@app.post("/arm/salute")
async def arm_salute() -> dict:
    _require_not_estopped()
    return {"result": await controller.arm_preset("salute", side="right")}


@app.post("/arm/home")
async def arm_home() -> dict:
    """Return arms to home (hanging at sides) and release control back
    to LocoClient so the next walk command isn't fought by arm_sdk."""
    _require_not_estopped()
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
