"""go2-style motion API for the Yahboom ROSMASTER R2 (Ackerman) chassis.

Wraps Yahboom's `Rosmaster_Lib` (talks to the STM32 driver board over
/dev/ttyUSB0) behind a small HTTP control plane, the same shape as the Go2
`motion` service: the web UI POSTs throttle/steer, this process owns the
serial link, and a watchdog stops the car if commands stop arriving.

Safety lives here, next to the hardware:
  * throttle/steer are clamped to MAX_SPEED / MAX_STEER_DEG
  * a watchdog stops the car WATCHDOG_S after the last /drive
The board connection is lazy + tolerant: if the board is absent the API still
comes up (so the app group runs for debugging) and /drive returns 503.
"""
import asyncio
import logging
import os
import sys

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
import uvicorn

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("motion")

PORT = int(os.environ.get("PORT", "3201"))
# "auto" probes every /dev/ttyUSB* and picks the one that answers as a ROSMASTER
# board (returns battery/version). This matters because the car exposes several
# USB-serial devices (the board, a 2nd board, the LiDAR) that claim ttyUSB0/1/2
# in a non-deterministic order — a fixed node often lands on the wrong device.
COM = os.environ.get("ROSMASTER_COM", "auto")
CARTYPE_R2 = 0x05

# Conservative limits — well under the R2's set_car_motion v_x=[-1.8,1.8] and
# v_z=[-3,3]. Override via env to open up the envelope.
MAX_SPEED = float(os.environ.get("MAX_SPEED", "0.6"))        # m/s at full throttle
# Steering is a PWM servo on port S1 (set_pwm_servo): 0=left, 90=centre, 180=right.
# (set_akm_steering_angle / set_car_motion v_z do NOT steer this chassis.)
STEER_SERVO = int(os.environ.get("STEER_SERVO", "1"))        # PWM servo port
STEER_CENTER = int(os.environ.get("STEER_CENTER", "90"))     # centre angle
STEER_RANGE = int(os.environ.get("STEER_RANGE", "70"))       # max deflection from centre
STEER_SIGN = int(os.environ.get("STEER_SIGN", "1"))          # flip if left/right swapped
WATCHDOG_S = float(os.environ.get("WATCHDOG_S", "0.6"))      # stop if no command within this

app = FastAPI(title="rc-car-motion")

_bot = None
_selected_com = None
_last_cmd = 0.0


RECONNECT_S = float(os.environ.get("RECONNECT_S", "3"))


def _candidates():
    if COM != "auto":
        return [COM]
    import glob
    return sorted(glob.glob("/dev/ttyUSB*")) or ["/dev/ttyUSB0"]


def _probe(dev):
    """Open dev as a Rosmaster, enable telemetry, and return the bot only if it
    actually answers (real battery voltage or firmware version). Otherwise None —
    so we don't mistake the LiDAR / 2nd board for the motor controller."""
    import time
    from Rosmaster_Lib import Rosmaster
    bot = Rosmaster(car_type=CARTYPE_R2, com=dev, debug=False)
    bot.create_receive_threading()
    bot.set_auto_report_state(True, forever=False)
    time.sleep(1.2)  # let telemetry frames arrive
    try:
        v = bot.get_battery_voltage()
        ver = bot.get_version()
    except Exception:  # noqa: BLE001
        v, ver = 0, -1
    if (v and v > 1.0) or (ver and ver > 0):
        return bot, v, ver
    return None, v, ver


def _connect():
    """Blocking: probe every candidate tty and latch onto the real ROSMASTER.
    Runs only from the background reconnect loop (in a thread), NEVER from a
    request handler — opening serial ports is synchronous and must not block
    the event loop."""
    global _bot, _selected_com
    if _bot is not None:
        return
    for dev in _candidates():
        try:
            bot, v, ver = _probe(dev)
            if bot is not None:
                _bot = bot
                _selected_com = dev
                logger.info("ROSMASTER board on %s (battery=%.1fV version=%s)", dev, v, ver)
                return
            logger.info("%s is not the ROSMASTER board (battery=%s version=%s) — skipping", dev, v, ver)
        except Exception as e:  # noqa: BLE001
            logger.warning("probe %s failed: %r", dev, e)
    _bot = None
    logger.warning("no ROSMASTER board found among %s (running degraded)", _candidates())


class DriveCmd(BaseModel):
    throttle: float = Field(0.0, ge=-1.0, le=1.0)  # -1 reverse .. +1 forward
    steer: float = Field(0.0, ge=-1.0, le=1.0)     # -1 left .. +1 right


def _apply(throttle: float, steer: float):
    bot = _bot  # cached; never connect from the request path
    if bot is None:
        raise HTTPException(status_code=503, detail=f"driver board not reachable on {COM}")
    vx = max(-1.0, min(1.0, throttle)) * MAX_SPEED
    s = max(-1.0, min(1.0, steer)) * STEER_SIGN
    servo = STEER_CENTER + int(round(s * STEER_RANGE))   # 0=left .. 90=centre .. 180=right
    servo = max(0, min(180, servo))
    bot.set_car_motion(vx, 0.0, 0.0)        # drive (rear motors)
    bot.set_pwm_servo(STEER_SERVO, servo)   # steer (front servo, S1)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_reconnect_loop())
    asyncio.create_task(_watchdog())


async def _reconnect_loop():
    """Attempt to (re)connect in the background, off the event loop, with a
    cooldown — so a missing board never turns request handling into a 10 Hz
    blocking-serial-open storm."""
    loop = asyncio.get_running_loop()
    while True:
        if _bot is None:
            await loop.run_in_executor(None, _connect)
        await asyncio.sleep(RECONNECT_S)


async def _watchdog():
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(WATCHDOG_S / 2)
        if _bot is not None and _last_cmd and (loop.time() - _last_cmd) > WATCHDOG_S:
            try:
                _bot.set_car_motion(0.0, 0.0, 0.0)
            except Exception:  # noqa: BLE001
                logger.exception("watchdog stop failed")


@app.post("/test")
async def test(req: Request):
    """Diagnostic: fire one steering primitive so we can see which actually moves
    the front wheels. Does NOT set _last_cmd, so the watchdog won't interfere."""
    body = await req.json()
    m = body.get("method", "")
    val = float(body.get("value", 0))
    bot = _bot
    if bot is None:
        raise HTTPException(status_code=503, detail="board not connected")
    try:
        if m == "akm":
            bot.set_akm_steering_angle(int(val))
        elif m == "akm_ctrl":
            bot.set_akm_steering_angle(int(val), ctrl_car=True)
        elif m == "default":
            bot.set_akm_default_angle(int(val))
        elif m == "pwm":
            bot.set_pwm_servo(int(body.get("id", 1)), int(val))
        elif m == "motion_vz":
            bot.set_car_motion(0.0, 0.0, val)
        elif m == "motion_vx":
            bot.set_car_motion(val, 0.0, 0.0)
        else:
            raise HTTPException(status_code=400, detail=f"unknown method {m}")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "method": m, "value": val, "error": repr(e)}
    return {"ok": True, "method": m, "value": val}


@app.post("/drive")
async def drive(cmd: DriveCmd):
    global _last_cmd
    _apply(cmd.throttle, cmd.steer)
    _last_cmd = asyncio.get_running_loop().time()
    return {"ok": True, "throttle": cmd.throttle, "steer": cmd.steer}


@app.post("/stop")
async def stop():
    bot = _bot
    if bot is not None:
        bot.set_car_motion(0.0, 0.0, 0.0)
        bot.set_pwm_servo(STEER_SERVO, STEER_CENTER)
    return {"ok": True}


@app.get("/health")
def health():
    bot = _bot
    out = {"connected": bot is not None, "com": _selected_com or COM,
           "max_speed": MAX_SPEED, "steer_servo": STEER_SERVO,
           "steer_center": STEER_CENTER, "steer_range": STEER_RANGE}
    if bot is not None:
        try:
            out["version"] = bot.get_version()
            out["battery_voltage"] = bot.get_battery_voltage()
        except Exception as e:  # noqa: BLE001
            out["query_error"] = repr(e)
    return out


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
