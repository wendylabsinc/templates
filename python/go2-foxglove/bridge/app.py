"""go2-foxglove bridge — stream the Go2's DDS data into Foxglove.

Runs a single Foxglove WebSocket server (default ws://<device>:8765) and
republishes the Go2's CycloneDDS topics as Foxglove channels. The `camera`
service forwards JPEG frames here over localhost (POST /frame), so EVERYTHING —
LiDAR, pose, body state, UWB, and camera — shows up on ONE Foxglove connection
and one shippable layout.

Channels:
  /go2/points  foxglove.PointCloud       ← rt/utlidar/cloud_deskewed (Livox MID-360)
  /go2/pose    foxglove.PoseInFrame      ← rt/sportmodestate (position + orientation)
  /tf          foxglove.FrameTransform   ← odom → base_link
  /go2/camera  foxglove.CompressedImage  ← forwarded by the camera service
  /go2/state   json                      ← rt/lowstate + rt/sportmodestate (plots)
  /go2/uwb     json                      ← rt/uwbstate (range / seen / yaw)

DDS binds by ADDRESS via cyclonedds.xml (GO2_DDS_ADDRESS) — the Go2 Orin is
multi-homed, so an interface NAME is ambiguous and DDS can advertise the wrong
subnet. CYCLONEDDS_URI (set in the Dockerfile) is honoured by both the
unitree_sdk2py channel factory and the direct cyclonedds participant below.

UNVERIFIED on a live Go2 EDU+. Verify: (1) the foxglove-sdk channel/schema API
against the version pinned in requirements.txt; (2) the DDS topic/field names on
your firmware (see the go2-initial-test template for the same caveats).

NOTE: do NOT add `from __future__ import annotations` — cyclonedds's IdlStruct
resolves type hints by name at class-definition time and PEP-563 breaks it.
"""
import logging
import os
import threading
import time

import uvicorn
from fastapi import FastAPI, Request, Response

import foxglove
from foxglove.channels import (
    CompressedImageChannel,
    FrameTransformChannel,
    PointCloudChannel,
    PoseInFrameChannel,
)
from foxglove.schemas import (
    CompressedImage,
    FrameTransform,
    PackedElementField,
    PackedElementFieldNumericType,
    PointCloud,
    Pose,
    PoseInFrame,
    Quaternion,
    Timestamp,
    Vector3,
)

from cyclonedds.core import Policy, Qos
from cyclonedds.domain import DomainParticipant
from cyclonedds.sub import DataReader, Subscriber
from cyclonedds.topic import Topic
from pointcloud2 import PointCloud2_  # local IDL (sensor_msgs/PointCloud2)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("go2-foxglove-bridge")

FOXGLOVE_PORT = int(os.environ.get("FOXGLOVE_PORT", "8765"))
INGEST_PORT = int(os.environ.get("INGEST_PORT", "8766"))
LIDAR_TOPIC = os.environ.get("LIDAR_TOPIC", "rt/utlidar/cloud_deskewed")
DDS_DOMAIN = int(os.environ.get("DDS_DOMAIN", "0"))

# ── Foxglove server + channels ──────────────────────────────────────────────
foxglove.set_log_level("INFO")
server = foxglove.start_server(host="0.0.0.0", port=FOXGLOVE_PORT, name="go2-foxglove")

points_ch = PointCloudChannel(topic="/go2/points")
pose_ch = PoseInFrameChannel(topic="/go2/pose")
tf_ch = FrameTransformChannel(topic="/tf")
camera_ch = CompressedImageChannel(topic="/go2/camera")
state_ch = foxglove.Channel(topic="/go2/state")  # json
uwb_ch = foxglove.Channel(topic="/go2/uwb")       # json

# ROS sensor_msgs/PointField datatype → foxglove PackedElementFieldNumericType.
# (The two enums number their types differently, so map explicitly.)
_T = PackedElementFieldNumericType
_ROS_TO_FOX = {1: _T.Int8, 2: _T.Uint8, 3: _T.Int16, 4: _T.Uint16,
               5: _T.Int32, 6: _T.Uint32, 7: _T.Float32, 8: _T.Float64}

_state = {}  # merged latest lowstate + sportmodestate fields for the plots

# Diagnostics, exposed at GET /diag on the ingest port — container logs are
# unreliable here, so this is how we see what's actually flowing.
_diag = {
    "unitree_init": "pending",
    "counts": {"lidar": 0, "lowstate": 0, "sport": 0, "uwb": 0, "camera": 0},
    "errors": [],
}


def _bump(k):
    _diag["counts"][k] = _diag["counts"].get(k, 0) + 1


def _err(where, e):
    msg = f"{where}: {type(e).__name__}: {e}"
    if msg not in _diag["errors"]:
        _diag["errors"].append(msg)
    print("DIAG-ERR", msg, flush=True)


def _now():
    t = time.time()
    return Timestamp(sec=int(t), nsec=int((t % 1) * 1e9))


def _safe(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001 — never let one bad frame kill a subscriber
        log.exception("publish failed")


# ── camera frame ingest (the camera container POSTs JPEGs here) ──────────────
api = FastAPI(title="go2-foxglove-ingest")


@api.post("/frame")
async def frame(request: Request):
    jpg = await request.body()
    if jpg:
        _safe(lambda: camera_ch.log(
            CompressedImage(timestamp=_now(), frame_id="camera", format="jpeg", data=jpg)))
        _bump("camera")
    return Response(status_code=204)


@api.get("/healthz")
def healthz():
    return {"ok": True}


@api.get("/diag")
def diag():
    return _diag


# ── DDS: point cloud (direct cyclonedds; PointCloud2 isn't a unitree msg) ────
def _lidar_loop():
    try:
        qos = Qos(Policy.Reliability.BestEffort, Policy.History.KeepLast(2))  # Go2 LiDAR is BEST_EFFORT
        dp = DomainParticipant(DDS_DOMAIN)
        reader = DataReader(Subscriber(dp), Topic(dp, LIDAR_TOPIC, PointCloud2_, qos=qos), qos=qos)
    except Exception as e:  # noqa: BLE001
        _err("lidar_setup", e)
        return
    while True:
        try:
            for msg in reader.take_iter(timeout=1_000_000_000):
                fields = [PackedElementField(name=f.name, offset=f.offset,
                                             type=_ROS_TO_FOX.get(f.datatype, _T.Float32))
                          for f in msg.fields]
                _safe(lambda m=msg, fl=fields: points_ch.log(PointCloud(
                    timestamp=_now(),
                    frame_id=m.header.frame_id or "base_link",
                    pose=Pose(position=Vector3(x=0, y=0, z=0),
                              orientation=Quaternion(x=0, y=0, z=0, w=1)),
                    point_stride=m.point_step,
                    fields=fl,
                    data=bytes(m.data),
                )))
                _bump("lidar")
        except Exception as e:  # noqa: BLE001
            _err("lidar_read", e)
            time.sleep(0.5)


# ── DDS: lowstate / sportmodestate / uwb via unitree_sdk2py ─────────────────
def _on_lowstate(msg):
    _bump("lowstate")
    try:
        imu = msg.imu_state
        _state.update(soc=int(msg.bms_state.soc), voltage=float(msg.power_v),
                      rpy=[round(float(v), 4) for v in imu.rpy],
                      gyro=[round(float(v), 4) for v in imu.gyroscope],
                      foot_force=[int(f) for f in msg.foot_force])
    except Exception as e:  # noqa: BLE001
        _err("lowstate_parse", e); return
    _safe(lambda: state_ch.log(dict(_state)))


def _on_sport(msg):
    _bump("sport")
    try:
        pos = [float(v) for v in msg.position[:3]]
        q = msg.imu_state.quaternion  # [w, x, y, z]
        _state.update(position=[round(p, 4) for p in pos],
                      velocity=[round(float(v), 4) for v in msg.velocity[:3]])
        fox_q = Quaternion(x=q[1], y=q[2], z=q[3], w=q[0])
    except Exception as e:  # noqa: BLE001
        _err("sport_parse", e); return
    ts = _now()
    _safe(lambda: pose_ch.log(PoseInFrame(timestamp=ts, frame_id="odom",
                                          pose=Pose(position=Vector3(x=pos[0], y=pos[1], z=pos[2]),
                                                    orientation=fox_q))))
    _safe(lambda: tf_ch.log(FrameTransform(timestamp=ts, parent_frame_id="odom",
                                           child_frame_id="base_link",
                                           translation=Vector3(x=pos[0], y=pos[1], z=pos[2]),
                                           rotation=fox_q)))
    _safe(lambda: state_ch.log(dict(_state)))


def _on_uwb(msg):
    _bump("uwb")
    try:
        d = {"seen": bool(msg.is_seen), "dist": round(float(msg.dist), 3),
             "yaw_est": round(float(getattr(msg, "yaw_est", 0.0)), 4)}
    except Exception as e:  # noqa: BLE001
        _err("uwb_parse", e); return
    _safe(lambda: uwb_ch.log(d))


def _start_unitree_subs():
    # Each step is isolated so one failure doesn't blank the others, and the
    # result lands in _diag (visible at GET /diag).
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_, SportModeState_, UwbState_
    except Exception as e:  # noqa: BLE001
        _diag["unitree_init"] = "import_failed"; _err("sdk_import", e); return
    try:
        ChannelFactoryInitialize(0)  # honours CYCLONEDDS_URI from the Dockerfile
    except Exception as e:  # noqa: BLE001
        _diag["unitree_init"] = "factory_failed"; _err("factory_init", e); return
    global _subs
    _subs = []
    for topic, typ, cb in (("rt/lowstate", LowState_, _on_lowstate),
                           ("rt/sportmodestate", SportModeState_, _on_sport),
                           ("rt/uwbstate", UwbState_, _on_uwb)):
        try:
            s = ChannelSubscriber(topic, typ)
            s.Init(cb, 10)
            _subs.append(s)
        except Exception as e:  # noqa: BLE001
            _err(f"sub_{topic}", e)
    _diag["unitree_init"] = f"ok ({len(_subs)}/3 subscribed)"
    print("DIAG", _diag["unitree_init"], flush=True)


def main():
    # Init the SDK factory FIRST, then the direct-cyclonedds LiDAR participant —
    # in case the dual DDS stack is order-sensitive in one process.
    _start_unitree_subs()
    threading.Thread(target=_lidar_loop, name="lidar", daemon=True).start()
    print(f"DIAG bridge up: foxglove :{FOXGLOVE_PORT}, ingest+diag :{INGEST_PORT}", flush=True)
    uvicorn.run(api, host="0.0.0.0", port=INGEST_PORT, log_level="warning")


if __name__ == "__main__":
    main()
