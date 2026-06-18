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

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("motion")

PORT = int(os.environ.get("PORT", "3201"))
COM = os.environ.get("ROSMASTER_COM", "/dev/ttyUSB0")
CARTYPE_R2 = 0x05

# Conservative limits — well under the R2's set_car_motion v_x=[-1.8,1.8] and
# the ±45° steering range. Override via env to open up the envelope.
MAX_SPEED = float(os.environ.get("MAX_SPEED", "0.6"))        # m/s at full throttle
MAX_STEER_DEG = float(os.environ.get("MAX_STEER_DEG", "30"))  # degrees at full steer
WATCHDOG_S = float(os.environ.get("WATCHDOG_S", "0.6"))       # stop if no command within this

app = FastAPI(title="rc-car-motion")

_bot = None
_last_cmd = 0.0


def _connect():
    """Open the Rosmaster board. Returns the bot or None (tolerant of absence)."""
    global _bot
    if _bot is not None:
        return _bot
    try:
        from Rosmaster_Lib import Rosmaster

        bot = Rosmaster(car_type=CARTYPE_R2, com=COM, debug=False)
        bot.create_receive_threading()
        _bot = bot
        logger.info("connected to ROSMASTER R2 on %s", COM)
    except Exception as e:  # noqa: BLE001
        logger.warning("could not open %s: %r (running degraded)", COM, e)
        _bot = None
    return _bot


class DriveCmd(BaseModel):
    throttle: float = Field(0.0, ge=-1.0, le=1.0)  # -1 reverse .. +1 forward
    steer: float = Field(0.0, ge=-1.0, le=1.0)     # -1 left .. +1 right


def _apply(throttle: float, steer: float):
    bot = _connect()
    if bot is None:
        raise HTTPException(status_code=503, detail=f"driver board not reachable on {COM}")
    vx = max(-1.0, min(1.0, throttle)) * MAX_SPEED
    angle = max(-1.0, min(1.0, steer)) * MAX_STEER_DEG
    bot.set_akm_steering_angle(int(angle))
    bot.set_car_motion(vx, 0.0, 0.0)


@app.on_event("startup")
async def _startup():
    _connect()
    asyncio.create_task(_watchdog())


async def _watchdog():
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(WATCHDOG_S / 2)
        if _bot is not None and _last_cmd and (loop.time() - _last_cmd) > WATCHDOG_S:
            try:
                _bot.set_car_motion(0.0, 0.0, 0.0)
            except Exception:  # noqa: BLE001
                logger.exception("watchdog stop failed")


@app.post("/drive")
async def drive(cmd: DriveCmd):
    global _last_cmd
    _apply(cmd.throttle, cmd.steer)
    _last_cmd = asyncio.get_running_loop().time()
    return {"ok": True, "throttle": cmd.throttle, "steer": cmd.steer}


@app.post("/stop")
async def stop():
    bot = _connect()
    if bot is not None:
        bot.set_car_motion(0.0, 0.0, 0.0)
        bot.set_akm_steering_angle(0)
    return {"ok": True}


@app.get("/health")
def health():
    bot = _connect()
    out = {"connected": bot is not None, "com": COM,
           "max_speed": MAX_SPEED, "max_steer_deg": MAX_STEER_DEG}
    if bot is not None:
        try:
            out["version"] = bot.get_version()
            out["battery_voltage"] = bot.get_battery_voltage()
        except Exception as e:  # noqa: BLE001
            out["query_error"] = repr(e)
    return out


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
