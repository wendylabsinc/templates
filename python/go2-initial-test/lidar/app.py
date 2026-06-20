"""LiDAR test — subscribe to the Go2's PointCloud2 and confirm scans arrive.

Direct CycloneDDS (no ROS2). IDL + topic conventions adapted from
/demos/go2-camera/perception.py. Reports pass with points-per-scan when clouds
are flowing, fail (with topic/interface hints) when nothing arrives.

NOTE: do NOT add `from __future__ import annotations` — cyclonedds's IdlStruct
resolves type hints by name at class-definition time and PEP-563 breaks it.
"""
import os
import threading
import time
from dataclasses import dataclass, field

import uvicorn
from fastapi import FastAPI

from cyclonedds.core import Policy, Qos
from cyclonedds.domain import DomainParticipant
from cyclonedds.idl import IdlStruct
from cyclonedds.idl.types import sequence, uint8, uint32, int32
from cyclonedds.sub import DataReader, Subscriber
from cyclonedds.topic import Topic

PORT = int(os.environ.get("PORT", "3613"))
LIDAR_TOPIC = os.environ.get("LIDAR_TOPIC", "rt/utlidar/cloud_deskewed")
DDS_DOMAIN = int(os.environ.get("DDS_DOMAIN", "0"))
FRESH_S = float(os.environ.get("LIDAR_FRESH_S", "3.0"))
DDS_ADDR = os.environ.get("GO2_DDS_ADDRESS", "").strip()  # for the error message only


@dataclass
class _Time(IdlStruct, typename="builtin_interfaces::msg::dds_::Time_"):
    sec: int32 = 0
    nanosec: uint32 = 0


@dataclass
class _Header(IdlStruct, typename="std_msgs::msg::dds_::Header_"):
    stamp: _Time = field(default_factory=_Time)
    frame_id: str = ""


@dataclass
class _PointField(IdlStruct, typename="sensor_msgs::msg::dds_::PointField_"):
    name: str = ""
    offset: uint32 = 0
    datatype: uint8 = 0
    count: uint32 = 0


@dataclass
class _PointCloud2(IdlStruct, typename="sensor_msgs::msg::dds_::PointCloud2_"):
    header: _Header = field(default_factory=_Header)
    height: uint32 = 0
    width: uint32 = 0
    fields: sequence[_PointField] = field(default_factory=list)
    is_bigendian: bool = False
    point_step: uint32 = 0
    row_step: uint32 = 0
    data: sequence[uint8] = field(default_factory=list)
    is_dense: bool = False


app = FastAPI(title="go2-test-lidar")
_last = {"n": 0, "frame": "", "ts": 0.0}
_err: str | None = None


def _run():
    global _err, _last
    while True:  # outer loop: (re)build the participant on any setup/read failure
        try:
            dp = DomainParticipant(DDS_DOMAIN)
            # LiDAR MUST be BEST_EFFORT — the Unitree driver won't deliver to a
            # RELIABLE subscriber.
            qos = Qos(Policy.Reliability.BestEffort, Policy.History.KeepLast(4))
            topic = Topic(dp, LIDAR_TOPIC, _PointCloud2, qos=qos)
            reader = DataReader(Subscriber(dp), topic, qos=qos)
            _err = None
        except Exception as e:  # noqa: BLE001
            # Most common off-robot cause: GO2_DDS_ADDRESS isn't an IP on any local
            # NIC, so CycloneDDS hard-fails at DomainParticipant init with a raw
            # DDS_RETCODE_ERROR. Translate it into the same actionable hint the other
            # tiles give instead of surfacing the raw error.
            _err = (f"can't start DDS bound to {DDS_ADDR or 'the configured NIC'} — is this device on "
                    f"the Go2 robot LAN (192.168.123.x) and is GO2_DDS_ADDRESS this machine's IP there? "
                    f"[{type(e).__name__}]")
            time.sleep(1.0)
            continue
        while True:
            try:
                for msg in reader.take_iter(timeout=1_000_000_000):  # 1 s
                    # swap a whole new dict in (atomic vs the request-thread reader)
                    _last = {"n": int(msg.width * msg.height),
                             "frame": msg.header.frame_id, "ts": time.time()}
            except Exception as e:  # noqa: BLE001
                _err = f"reader error: {e}"
                time.sleep(1.0)
                break  # rebuild the participant


@app.on_event("startup")
def _startup():
    threading.Thread(target=_run, name="lidar-sub", daemon=True).start()


def _result():
    s = _last  # snapshot the dict reference (the reader thread swaps, never mutates)
    fresh = s["ts"] and (time.time() - s["ts"]) < FRESH_S
    if fresh and s["n"] > 0:
        return {"interface": "lidar", "status": "pass",
                "detail": f"{s['n']} pts/scan on {LIDAR_TOPIC} (frame {s['frame'] or '?'})",
                "data": {"points": s["n"], "frame": s["frame"]}}
    detail = _err or (f"no PointCloud2 on {LIDAR_TOPIC} (DDS bound {DDS_ADDR or '?'}) — is the Go2 "
                      "LiDAR on and this device on the robot LAN (192.168.123.x)? "
                      "(alt topic: LIDAR_TOPIC=rt/utlidar/cloud_undeskewed)")
    return {"interface": "lidar", "status": "fail", "detail": detail, "data": {}}


@app.get("/status")
def status():
    return {"results": [_result()]}


@app.post("/run")
def rerun():
    r = _result()
    return {"ok": r["status"] == "pass", "results": [r]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
