"""robot-state test hub — IMU, foot contact, battery, joints, odometry, remote, UWB.

One SDK process subscribes to several topics and reports the interfaces. Adapted
from /demos/go2-motion/go2_controller.py.

Diagnostic integrity (review round 2): callbacks record their parse error instead
of silently swallowing it, so "frames arriving but parse failed: <e>" is
distinguishable from "no frames / robot off".
"""
import asyncio
import logging
import os
import time

import uvicorn
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("go2-test-lowstate")

PORT = int(os.environ.get("PORT", "3611"))
IFACE = os.environ.get("GO2_NETWORK_INTERFACE", "eth0")
DDS_ADDR = os.environ.get("GO2_DDS_ADDRESS", "").strip()
FRESH_S = float(os.environ.get("LOWSTATE_FRESH_S", "3.0"))
CONTACT_N = float(os.environ.get("FOOT_CONTACT_N", "50"))
N_JOINTS = int(os.environ.get("N_JOINTS", "12"))
TEMP_WARN_C = float(os.environ.get("MOTOR_TEMP_WARN_C", "75"))

app = FastAPI(title="go2-test-lowstate")
_factory_initialized = False
_subs = []  # keep subscriber refs alive (avoid GC teardown)

_low, _low_ts, _low_err = {}, 0.0, None
_sport, _sport_ts, _sport_err = {}, 0.0, None
_remote, _remote_ts, _remote_err = {}, 0.0, None
_uwb, _uwb_ts = {}, 0.0
_err = None


def _on_lowstate(msg):
    global _low, _low_ts, _low_err
    try:
        rpy = list(msg.imu_state.rpy) if msg.imu_state.rpy else [0.0, 0.0, 0.0]
        motors = [{"q": round(float(m.q), 4), "dq": round(float(m.dq), 4),
                   "tau": round(float(m.tau_est), 3), "temp": int(m.temperature)}
                  for m in list(msg.motor_state)[:N_JOINTS]]
        _low = {"battery_soc": int(msg.bms_state.soc), "power_v": float(msg.power_v),
                "imu_rpy": [round(v, 4) for v in rpy], "foot_force": list(msg.foot_force),
                "motors": motors}
        _low_ts, _low_err = time.time(), None
    except Exception as e:  # noqa: BLE001
        _low_err = repr(e)
        logger.warning("LowState parse failed: %s", e)


def _on_sport(msg):
    global _sport, _sport_ts, _sport_err
    try:
        yaw = round(float(msg.imu_state.rpy[2]), 4) if getattr(msg, "imu_state", None) and msg.imu_state.rpy else 0.0
        _sport = {"position": [round(float(v), 3) for v in list(msg.position)[:3]],
                  "velocity": [round(float(v), 3) for v in list(msg.velocity)[:3]],
                  "yaw": yaw, "gait_type": int(getattr(msg, "gait_type", 0))}
        _sport_ts, _sport_err = time.time(), None
    except Exception as e:  # noqa: BLE001
        _sport_err = repr(e)
        logger.warning("SportModeState parse failed: %s", e)


def _on_wireless(msg):
    global _remote, _remote_ts, _remote_err
    try:
        _remote = {"lx": round(float(msg.lx), 3), "ly": round(float(msg.ly), 3),
                   "rx": round(float(msg.rx), 3), "ry": round(float(msg.ry), 3), "keys": int(msg.keys)}
        _remote_ts, _remote_err = time.time(), None
    except Exception as e:  # noqa: BLE001
        _remote_err = repr(e)
        logger.warning("WirelessController parse failed: %s", e)


def _on_uwb(msg):
    global _uwb, _uwb_ts
    try:
        _uwb = {"is_seen": bool(msg.is_seen), "dist": round(float(msg.dist), 3),
                "yaw_est": round(float(getattr(msg, "yaw_est", 0.0)), 4)}
        _uwb_ts = time.time()
    except Exception as e:  # noqa: BLE001
        logger.warning("UwbState parse failed: %s", e)


def _connect():
    global _err, _factory_initialized
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import (
            LowState_, SportModeState_, WirelessController_,
        )
        if not _factory_initialized:
            if DDS_ADDR:
                # Bind DDS to this device's IP — the Go2 Orin is multi-homed.
                os.environ["CYCLONEDDS_URI"] = (
                    "<CycloneDDS><Domain><General><Interfaces>"
                    f'<NetworkInterface address="{DDS_ADDR}"/>'
                    "</Interfaces></General></Domain></CycloneDDS>"
                )
                ChannelFactoryInitialize(0)
            else:
                ChannelFactoryInitialize(0, IFACE)
            _factory_initialized = True
        for topic, typ, cb in (("rt/lowstate", LowState_, _on_lowstate),
                               ("rt/sportmodestate", SportModeState_, _on_sport),
                               ("rt/wirelesscontroller", WirelessController_, _on_wireless)):
            s = ChannelSubscriber(topic, typ)
            s.Init(cb, 10)
            _subs.append(s)
        # UWB is optional — import + subscribe separately so a missing UwbState_
        # in the pinned SDK can't take down the other five interfaces.
        try:
            from unitree_sdk2py.idl.unitree_go.msg.dds_ import UwbState_
            s = ChannelSubscriber("rt/uwbstate", UwbState_)
            s.Init(_on_uwb, 10)
            _subs.append(s)
        except Exception as e:  # noqa: BLE001
            logger.info("UWB subscriber not available: %s", e)
    except Exception as e:  # noqa: BLE001
        _err = f"DDS init failed: {e}"


@app.on_event("startup")
async def _startup():
    await asyncio.to_thread(_connect)


def _fresh(ts):
    return bool(ts and (time.time() - ts) < FRESH_S)


def _down_detail(parse_err):
    if parse_err:
        return f"frames arriving but parse failed: {parse_err}"
    return _err or (f"no frames (DDS bind {DDS_ADDR or IFACE}) — is the Go2 powered, on the robot "
                    "LAN, and GO2_DDS_ADDRESS set to this device's 192.168.123.x IP?")


def _results():
    out = []
    if _fresh(_low_ts):
        s = _low
        ff = s.get("foot_force", [])
        contacts = sum(1 for f in ff if f > CONTACT_N)
        soc, v = s.get("battery_soc", 0), s.get("power_v", 0.0)
        motors = s.get("motors", [])
        temps = [m["temp"] for m in motors]
        max_t = max(temps) if temps else 0
        # soc==0 on a running robot is almost always a transient/unpopulated read —
        # don't flip to CRITICAL fail on it. (power_v often reads 0 too; show n/a.)
        batt_crit = 0 < soc < 5
        vtxt = f"{v:.1f} V" if v > 0 else "voltage n/a"
        batt_note = (" (soc reads 0 — transient/unpopulated?)" if soc == 0
                     else " · CRITICAL" if batt_crit else " · LOW" if soc < 15 else "")
        out += [
            {"interface": "imu", "status": "pass", "detail": f"rpy = {s.get('imu_rpy')} rad",
             "data": {"rpy": s.get("imu_rpy")}},
            {"interface": "foot_contact", "status": "pass",
             "detail": f"forces = {ff} N · {contacts}/4 in contact", "data": {"foot_force": ff}},
            {"interface": "battery", "status": "fail" if batt_crit else "pass",
             "detail": f"{soc}% · {vtxt}{batt_note}", "data": {"soc": soc, "power_v": v}},
            {"interface": "joints",
             "status": "pass" if (motors and max_t < TEMP_WARN_C) else "fail",
             "detail": (f"{len(motors)} joints · max motor temp {max_t}°C"
                        + (" · HOT" if max_t >= TEMP_WARN_C else "")) if motors else "no motor_state in LowState",
             "data": {"temps": temps}},
        ]
    else:
        d = _down_detail(_low_err)
        out += [{"interface": k, "status": "fail", "detail": d, "data": {}}
                for k in ("imu", "foot_contact", "battery", "joints")]

    if _fresh(_sport_ts):
        sp = _sport
        out.append({"interface": "odometry", "status": "pass",
                    "detail": f"pos={sp['position']} m · vel={sp['velocity']} m/s · gait {sp['gait_type']}",
                    "data": sp})
    else:
        # `na` (not fail) when the topic simply isn't flowing — rt/sportmodestate only
        # streams in high-level sport mode, so an idle/low-level dog shouldn't go red.
        # A genuine parse error (frames arriving, parse failing) is still a fail.
        out.append({"interface": "odometry", "status": "fail" if _sport_err else "na",
                    "detail": _down_detail(_sport_err) if _sport_err else
                    "no SportModeState on rt/sportmodestate (high-level sport mode not active?)", "data": {}})

    if _fresh(_remote_ts):
        rm = _remote
        out.append({"interface": "remote", "status": "pass",
                    "detail": f"controller live · sticks L({rm['lx']},{rm['ly']}) R({rm['rx']},{rm['ry']}) keys={rm['keys']}",
                    "data": rm})
    else:
        # `na` (not fail) when no controller frames — the handheld remote being off
        # is the normal state and must not turn the whole board red. Parse error = fail.
        out.append({"interface": "remote", "status": "fail" if _remote_err else "na",
                    "detail": _down_detail(_remote_err) if _remote_err else
                    "no controller frames — turn the handheld remote on and move a stick", "data": {}})

    if _fresh(_uwb_ts):
        u = _uwb
        out.append({"interface": "uwb", "status": "pass",
                    "detail": (f"tag seen @ {u['dist']} m, yaw {u['yaw_est']} rad" if u["is_seen"]
                               else "UWB module present · no tag in range"), "data": u})
    else:
        out.append({"interface": "uwb", "status": "na",
                    "detail": "no rt/uwbstate frames — UWB is an optional paid accessory (not fitted?)", "data": {}})
    return out


@app.get("/status")
def status():
    return {"results": _results()}


@app.post("/run")
def rerun():
    return {"ok": _fresh(_low_ts), "results": _results()}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
