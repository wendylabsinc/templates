"""Joystick/gamepad driver for the rc-car app group.

Reads a USB or Bluetooth gamepad via the Linux joydev interface
(/dev/input/js0) and forwards throttle/steer to the `motion` service's /drive
endpoint at a steady rate (so the on-car watchdog stays fed while a stick is
held). Also exposes /health for debugging — including the live axis/button
state, so you can map an unknown gamepad.

Entitlements: network (reach motion on localhost), input (read /dev/input/js*),
bluetooth (let a BT gamepad pair/connect).
"""
import json
import os
import struct
import sys
import threading
import time

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

PORT = int(os.environ.get("PORT", "3600"))
JS_DEV = os.environ.get("JOYSTICK_DEV", "/dev/input/js0")
MOTION_URL = os.environ.get("MOTION_URL", "http://127.0.0.1:3201").rstrip("/")
THROTTLE_AXIS = int(os.environ.get("THROTTLE_AXIS", "1"))   # left stick Y
STEER_AXIS = int(os.environ.get("STEER_AXIS", "0"))         # left stick X
THROTTLE_SIGN = float(os.environ.get("THROTTLE_SIGN", "-1"))  # stick up = forward
STEER_SIGN = float(os.environ.get("STEER_SIGN", "1"))
DEADZONE = float(os.environ.get("DEADZONE", "0.08"))
SEND_HZ = float(os.environ.get("SEND_HZ", "15"))
ESTOP_BUTTON = int(os.environ.get("ESTOP_BUTTON", "1"))     # a button that forces stop

app = FastAPI(title="rc-car-joystick")

_axes = {}
_buttons = {}
_lock = threading.Lock()
_state = {"js_connected": False, "device": JS_DEV, "last_throttle": 0.0, "last_steer": 0.0}


def _reader():
    """Blocking joydev reader. 8-byte events: uint32 time, int16 value, uint8 type, uint8 number."""
    while True:
        try:
            with open(JS_DEV, "rb") as f:
                _state["js_connected"] = True
                while True:
                    evt = f.read(8)
                    if len(evt) < 8:
                        break
                    _t, value, typ, num = struct.unpack("IhBB", evt)
                    typ &= ~0x80  # strip JS_EVENT_INIT
                    with _lock:
                        if typ == 0x02:      # axis
                            _axes[num] = max(-1.0, min(1.0, value / 32767.0))
                        elif typ == 0x01:    # button
                            _buttons[num] = value
        except FileNotFoundError:
            _state["js_connected"] = False
            time.sleep(2)            # gamepad not connected yet
        except Exception:            # noqa: BLE001
            _state["js_connected"] = False
            time.sleep(1)


def _sender():
    client = httpx.Client(timeout=0.4)
    period = 1.0 / max(1.0, SEND_HZ)
    was_active = False
    while True:
        with _lock:
            thr = _axes.get(THROTTLE_AXIS, 0.0) * THROTTLE_SIGN
            st = _axes.get(STEER_AXIS, 0.0) * STEER_SIGN
            estop = _buttons.get(ESTOP_BUTTON, 0)
        if abs(thr) < DEADZONE:
            thr = 0.0
        if abs(st) < DEADZONE:
            st = 0.0
        if estop:
            thr = 0.0
        thr = max(-1.0, min(1.0, thr))
        st = max(-1.0, min(1.0, st))
        _state["last_throttle"], _state["last_steer"] = thr, st
        active = thr != 0.0 or st != 0.0 or bool(estop)
        # Command while the stick is active, plus one final stop on release — so
        # an idle joystick doesn't fight the web UI (and the watchdog handles idle).
        if _state["js_connected"] and (active or was_active):
            try:
                client.post(f"{MOTION_URL}/drive", json={"throttle": thr, "steer": st})
            except httpx.HTTPError:
                pass
        was_active = active
        time.sleep(period)


@app.on_event("startup")
def _startup():
    threading.Thread(target=_reader, daemon=True).start()
    threading.Thread(target=_sender, daemon=True).start()


@app.get("/health")
def health():
    with _lock:
        axes = dict(_axes)
        buttons = dict(_buttons)
    return JSONResponse({**_state, "axes": axes, "buttons": buttons,
                         "throttle_axis": THROTTLE_AXIS, "steer_axis": STEER_AXIS})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
