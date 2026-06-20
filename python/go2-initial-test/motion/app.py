"""motion test service — all SportClient-based actuation tests. MANUAL only.

Hosts (the dashboard POSTs /run with {"interface": <key>}):
  motion         — walk ~0.5 m forward + back
  posture        — gentle posture/gait sequence
  obstacle_avoid — toggle built-in obstacle avoidance and restore
  acrobatics     — ⚠ DANGER flip/jump (UI double-confirm AND ENABLE_ACROBATICS=1)

Safety: every moving test runs inside try/finally that ALWAYS issues StopMove —
including on client disconnect / asyncio.CancelledError — so closing the tab
mid-walk can't leave the dog running.
"""
import asyncio
import logging
import os

import uvicorn
from fastapi import FastAPI, Request

from go2_controller import Go2Controller

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("go2-test-motion")

PORT = int(os.environ.get("PORT", "3616"))
WALK_SPEED = float(os.environ.get("WALK_SPEED", "0.3"))
WALK_DIST = float(os.environ.get("WALK_DIST", "0.5"))
ACROBATIC_SKILL = os.environ.get("ACROBATIC_SKILL", "FrontFlip")
ENABLE_ACROBATICS = os.environ.get("ENABLE_ACROBATICS", "").lower() in ("1", "true", "yes")
MOVING = {"motion", "posture", "acrobatics"}

app = FastAPI(title="go2-test-motion")
_ctrl = Go2Controller()
_connected = False
_lock = asyncio.Lock()
_obstacle_client = None

_results = {
    "motion": {"interface": "motion", "status": "manual",
               "detail": "press “Run walk test” — robot walks ~0.5 m forward + back", "data": {}},
    "posture": {"interface": "posture", "status": "manual",
                "detail": "press to run a gentle posture/gait sequence (stand→balance→body-height→euler→sit→stand)",
                "data": {}},
    "obstacle_avoid": {"interface": "obstacle_avoid", "status": "manual",
                       "detail": "press to toggle built-in obstacle avoidance on/off and restore", "data": {}},
    "acrobatics": {"interface": "acrobatics", "status": "manual",
                   "detail": f"⚠ DANGER — runs {ACROBATIC_SKILL}. Needs a large clear soft area, EDU-only, "
                             "and ENABLE_ACROBATICS=1 on the motion service.", "data": {}},
}


async def _ensure():
    global _connected
    if not _connected:
        await asyncio.to_thread(_ctrl.connect)
        _connected = True


async def _safe_stop():
    try:
        await _ctrl.stop()
    except Exception:  # noqa: BLE001
        logger.exception("safety StopMove failed")


async def _do_walk():
    dur = WALK_DIST / max(0.05, WALK_SPEED)
    await _ensure()
    await _ctrl.stand_up()
    await _ctrl.move(vx=WALK_SPEED, vy=0.0, vyaw=0.0, duration=dur)
    await asyncio.sleep(0.3)
    await _ctrl.move(vx=-WALK_SPEED, vy=0.0, vyaw=0.0, duration=dur)
    return f"walked {WALK_DIST} m fwd + back @ {WALK_SPEED} m/s"


async def _do_posture():
    await _ensure()
    await _ctrl.stand_up()
    await _ctrl.skill("BalanceStand")
    # BodyHeight + Euler are SDK/firmware-dependent (the pinned SDK has no
    # BodyHeight). Run them best-effort so one missing method doesn't fail the
    # whole posture check; record any the SDK doesn't expose.
    skipped = []
    for name, args in (("BodyHeight", (0.1,)), ("BodyHeight", (0.0,)),
                       ("Euler", (0.0, 0.0, 0.2)), ("Euler", (0.0, 0.0, 0.0))):
        r = await _ctrl.skill(name, *args, optional=True)
        if "skipped" in r and name not in skipped:
            skipped.append(name)
    await _ctrl.lie_down()
    await _ctrl.stand_up()
    note = f" (not in this SDK, skipped: {', '.join(skipped)})" if skipped else ""
    return f"posture/gait sequence OK (stand→balance→body-height→euler→sit→stand){note}"


async def _do_obstacle():
    global _obstacle_client
    await _ensure()
    # ObstaclesAvoidClient API (SwitchGet/SwitchSet) is best-guess from the SDK.
    from unitree_sdk2py.go2.obstacles_avoid.obstacles_avoid_client import ObstaclesAvoidClient
    if _obstacle_client is None:
        c = ObstaclesAvoidClient()
        c.SetTimeout(3.0)
        c.Init()
        _obstacle_client = c
    _, cur = _obstacle_client.SwitchGet()
    _obstacle_client.SwitchSet(not cur)
    await asyncio.sleep(0.5)            # let the controller apply it
    _, mid = _obstacle_client.SwitchGet()
    _obstacle_client.SwitchSet(cur)     # restore original state
    await asyncio.sleep(0.2)
    confirmed = mid != cur
    return (f"obstacle-avoidance reachable (was {'on' if cur else 'off'}; "
            f"toggle {'confirmed' if confirmed else 'NOT observed'}); restored")


async def _do_acrobatics():
    if not ENABLE_ACROBATICS:
        raise RuntimeError("acrobatics disabled — set ENABLE_ACROBATICS=1 on the motion service to allow")
    await _ensure()
    await _ctrl.stand_up()
    await _ctrl.skill(ACROBATIC_SKILL)
    return f"{ACROBATIC_SKILL} executed"


_DISPATCH = {
    "motion": _do_walk, "posture": _do_posture,
    "obstacle_avoid": _do_obstacle, "acrobatics": _do_acrobatics,
}


@app.get("/status")
def status():
    return {"results": list(_results.values())}


@app.post("/run")
async def run(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    key = body.get("interface", "motion")
    fn = _DISPATCH.get(key)
    if fn is None:
        return {"ok": False, "error": f"unknown interface {key}"}
    async with _lock:
        _results[key] = {"interface": key, "status": "pending", "detail": f"{key} running…", "data": {}}
        try:
            detail = await fn()
            _results[key] = {"interface": key, "status": "pass", "detail": detail, "data": {}}
        except asyncio.CancelledError:
            _results[key] = {"interface": key, "status": "fail", "detail": "cancelled — stopping robot", "data": {}}
            raise
        except Exception as e:  # noqa: BLE001
            _results[key] = {"interface": key, "status": "fail", "detail": f"{key} failed: {e}", "data": {}}
        finally:
            if key in MOVING:
                await _safe_stop()  # always halt after a moving test, even on cancel
    return {"ok": _results[key]["status"] == "pass", "result": _results[key]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
