"""Canonical Go2 telemetry and front camera over one Foxglove connection.

This adapter deliberately does not forward the raw Go2 ROS/DDS graph. Some Go2
firmware advertises aliases such as ``rt/lowstate`` and ``rt/lf/lowstate`` with
metadata that generic ROS bridges expose as conflicting Foxglove channels. We
subscribe to explicit Unitree types, select one live alias, validate every
sample, and publish stable viewer-native channels instead.
"""

from __future__ import annotations

import copy
import logging
import math
import os
import threading
import time
from typing import Any

import foxglove
import uvicorn
from cyclonedds.core import Policy, Qos
from cyclonedds.domain import DomainParticipant
from cyclonedds.sub import DataReader, Subscriber
from cyclonedds.topic import Topic
from fastapi import FastAPI
from foxglove import Channel
from foxglove.channels import (
    CompressedImageChannel,
    FrameTransformChannel,
    JointStatesChannel,
    PointCloudChannel,
    PoseInFrameChannel,
)
from foxglove.messages import (
    CompressedImage,
    FrameTransform,
    JointState,
    JointStates,
    PackedElementField,
    PackedElementFieldNumericType,
    PointCloud,
    Pose,
    PoseInFrame,
    Quaternion,
    Timestamp,
    Vector3,
)
from pointcloud2 import PointCloud2_
from state import (
    StickySourceSelector,
    cyclonedds_config,
    normalize_lowstate,
    normalize_sportstate,
    resolve_dds_address,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("go2-foxglove")

FOXGLOVE_PORT = int(os.environ.get("FOXGLOVE_PORT", "8765"))
FOXGLOVE_BIND_HOST = os.environ.get("FOXGLOVE_BIND_HOST", "127.0.0.1")
DIAG_PORT = int(os.environ.get("DIAG_PORT", "8766"))
DIAG_BIND_HOST = os.environ.get("DIAG_BIND_HOST", "0.0.0.0")
GO2_IP = os.environ.get("GO2_IP", "192.168.123.161")
GO2_DDS_ADDRESS = os.environ.get("GO2_DDS_ADDRESS", "")
DDS_DOMAIN = int(os.environ.get("DDS_DOMAIN", "0"))
LIDAR_TOPIC = os.environ.get("LIDAR_TOPIC", "rt/utlidar/cloud_deskewed")
LOWSTATE_TOPICS = tuple(
    value.strip()
    for value in os.environ.get("LOWSTATE_TOPICS", "rt/lf/lowstate,rt/lowstate").split(
        ","
    )
    if value.strip()
)
SPORT_TOPICS = tuple(
    value.strip()
    for value in os.environ.get(
        "SPORT_TOPICS", "rt/lf/sportmodestate,rt/sportmodestate"
    ).split(",")
    if value.strip()
)
STATE_PUBLISH_HZ = float(os.environ.get("STATE_PUBLISH_HZ", "20"))
STATE_MAX_AGE_S = float(os.environ.get("STATE_MAX_AGE_S", "0.5"))
SPORT_MAX_AGE_S = float(os.environ.get("SPORT_MAX_AGE_S", "1.0"))
CAMERA_MAX_AGE_S = float(os.environ.get("CAMERA_MAX_AGE_S", "2.0"))
CAMERA_MAX_FPS = float(os.environ.get("CAMERA_MAX_FPS", "15"))
CAMERA_TIMEOUT_S = float(os.environ.get("CAMERA_TIMEOUT_S", "3.0"))
CAMERA_MAX_JPEG_BYTES = int(
    os.environ.get("CAMERA_MAX_JPEG_BYTES", str(8 * 1024 * 1024))
)

for name, value in (
    ("STATE_PUBLISH_HZ", STATE_PUBLISH_HZ),
    ("STATE_MAX_AGE_S", STATE_MAX_AGE_S),
    ("SPORT_MAX_AGE_S", SPORT_MAX_AGE_S),
    ("CAMERA_MAX_AGE_S", CAMERA_MAX_AGE_S),
    ("CAMERA_MAX_FPS", CAMERA_MAX_FPS),
):
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive")
if not LOWSTATE_TOPICS or not SPORT_TOPICS:
    raise ValueError("LOWSTATE_TOPICS and SPORT_TOPICS must not be empty")

DDS_ADDRESS = resolve_dds_address(GO2_IP, GO2_DDS_ADDRESS)
DDS_CONFIG = cyclonedds_config(DDS_ADDRESS)
# The direct CycloneDDS LiDAR participant honors this environment variable.
os.environ["CYCLONEDDS_URI"] = DDS_CONFIG

foxglove.set_log_level("INFO")
_server = foxglove.start_server(
    host=FOXGLOVE_BIND_HOST,
    port=FOXGLOVE_PORT,
    name="Unitree Go2 canonical observability",
)

_state_channel = Channel(
    topic="/go2/state",
    schema={
        "type": "object",
        "required": ["schema_version", "published_at_ns", "lowstate", "sport"],
    },
)
_health_channel = Channel(
    topic="/go2/health",
    schema={"type": "object", "required": ["ok", "dds", "state", "camera"]},
)
_uwb_channel = Channel(
    topic="/go2/uwb",
    schema={
        "type": "object",
        "required": ["received_at_ns", "distance_m", "yaw_rad", "error_state"],
    },
)
_joints_channel = JointStatesChannel(topic="/go2/joints")
_pose_channel = PoseInFrameChannel(topic="/go2/pose")
_tf_channel = FrameTransformChannel(topic="/tf")
_points_channel = PointCloudChannel(topic="/go2/points")
_camera_channel = CompressedImageChannel(topic="/go2/camera")

_lock = threading.RLock()
_low_selector = StickySourceSelector(STATE_MAX_AGE_S)
_sport_selector = StickySourceSelector(SPORT_MAX_AGE_S)
_low: dict[str, Any] | None = None
_sport: dict[str, Any] | None = None
_low_source: str | None = None
_sport_source: str | None = None
_low_monotonic = 0.0
_sport_monotonic = 0.0
_low_received = 0
_sport_received = 0
_state_published = 0
_rejected = {"lowstate": 0, "sport": 0, "uwb": 0, "lidar": 0, "camera": 0}
_last_error: dict[str, str | None] = {
    "dds": None,
    "lowstate": None,
    "sport": None,
    "uwb": None,
    "lidar": None,
    "camera": None,
}
_camera = {
    "frames": 0,
    "last_monotonic": 0.0,
    "last_at_ns": 0,
    "status": "starting",
    "failures": 0,
    "restarts": 0,
    "last_error": None,
    "last_error_at_ns": 0,
}
_lidar = {"frames": 0, "last_monotonic": 0.0, "status": "starting"}
_subscribers: list[Any] = []


def _timestamp(nanoseconds: int) -> Timestamp:
    return Timestamp(sec=nanoseconds // 1_000_000_000, nsec=nanoseconds % 1_000_000_000)


def _age(last_monotonic: float, now: float | None = None) -> float | None:
    if last_monotonic <= 0:
        return None
    return max(0.0, (time.monotonic() if now is None else now) - last_monotonic)


def _fresh(last_monotonic: float, max_age_s: float, now: float | None = None) -> bool:
    age = _age(last_monotonic, now)
    return age is not None and age <= max_age_s


def _record_error(component: str, error: Exception | str) -> None:
    rendered = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    with _lock:
        _last_error[component] = rendered
    logger.warning("%s: %s", component, rendered)


def _on_lowstate(source: str, message: Any) -> None:
    global _low, _low_source, _low_monotonic, _low_received
    now = time.monotonic()
    with _lock:
        accepted = _low_selector.accept(source, now)
        next_id = _low_received + 1
    if not accepted:
        return
    try:
        normalized = normalize_lowstate(
            message, received_at_ns=time.time_ns(), sample_id=next_id
        )
    except Exception as error:  # noqa: BLE001 - malformed DDS must not stop reception
        with _lock:
            _rejected["lowstate"] += 1
        _record_error("lowstate", error)
        return
    with _lock:
        _low = normalized
        _low_source = source
        _low_monotonic = now
        _low_received = next_id
        _last_error["lowstate"] = None


def _on_sport(source: str, message: Any) -> None:
    global _sport, _sport_source, _sport_monotonic, _sport_received
    now = time.monotonic()
    with _lock:
        accepted = _sport_selector.accept(source, now)
        next_id = _sport_received + 1
    if not accepted:
        return
    try:
        normalized = normalize_sportstate(
            message, received_at_ns=time.time_ns(), sample_id=next_id
        )
    except Exception as error:  # noqa: BLE001
        with _lock:
            _rejected["sport"] += 1
        _record_error("sport", error)
        return
    with _lock:
        _sport = normalized
        _sport_source = source
        _sport_monotonic = now
        _sport_received = next_id
        _last_error["sport"] = None


def _on_uwb(message: Any) -> None:
    try:
        received_at_ns = time.time_ns()
        payload = {
            "received_at_ns": received_at_ns,
            "distance_m": float(message.distance_est),
            "yaw_rad": float(getattr(message, "yaw_est", 0.0)),
            "error_state": int(getattr(message, "error_state", 0)),
            "enabled_from_app": int(getattr(message, "enabled_from_app", 0)),
        }
        if not all(math.isfinite(payload[key]) for key in ("distance_m", "yaw_rad")):
            raise ValueError("UWB distance or yaw is not finite")
        _uwb_channel.log(payload, log_time=received_at_ns)
        with _lock:
            _last_error["uwb"] = None
    except Exception as error:  # noqa: BLE001
        with _lock:
            _rejected["uwb"] += 1
        _record_error("uwb", error)


def _start_unitree() -> None:
    """Initialize one exact-address Unitree DDS participant and its readers."""

    try:
        import unitree_sdk2py.core.channel as unitree_channel
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_, SportModeState_

        # unitree_sdk2py passes an explicit auto-detect XML string to CycloneDDS,
        # which overrides CYCLONEDDS_URI. Replace that module-level config before
        # the singleton factory initializes so multi-homed Go2 hosts bind the
        # exact address selected by the kernel route to GO2_IP.
        unitree_channel.ChannelConfigAutoDetermine = DDS_CONFIG
        unitree_channel.ChannelFactoryInitialize(DDS_DOMAIN)
        for topic in LOWSTATE_TOPICS:
            subscriber = unitree_channel.ChannelSubscriber(topic, LowState_)
            subscriber.Init(
                lambda message, source=topic: _on_lowstate(source, message), 10
            )
            _subscribers.append(subscriber)
        for topic in SPORT_TOPICS:
            subscriber = unitree_channel.ChannelSubscriber(topic, SportModeState_)
            subscriber.Init(
                lambda message, source=topic: _on_sport(source, message), 10
            )
            _subscribers.append(subscriber)
        try:
            from unitree_sdk2py.idl.unitree_go.msg.dds_ import UwbState_

            subscriber = unitree_channel.ChannelSubscriber("rt/uwbstate", UwbState_)
            subscriber.Init(_on_uwb, 10)
            _subscribers.append(subscriber)
        except Exception as error:  # noqa: BLE001 - UWB is optional
            _record_error("uwb", error)
        with _lock:
            _last_error["dds"] = None
        logger.info(
            "Unitree DDS bound to %s; lowstate=%s sport=%s",
            DDS_ADDRESS,
            LOWSTATE_TOPICS,
            SPORT_TOPICS,
        )
    except Exception as error:
        _record_error("dds", error)
        raise


def _canonical_state(now: float) -> dict[str, Any]:
    with _lock:
        low = copy.deepcopy(_low)
        sport = copy.deepcopy(_sport)
        low_age = _age(_low_monotonic, now)
        sport_age = _age(_sport_monotonic, now)
        low_source = _low_source
        sport_source = _sport_source
    return {
        "schema_version": 1,
        "published_at_ns": time.time_ns(),
        "lowstate": {
            "fresh": low_age is not None and low_age <= STATE_MAX_AGE_S,
            "age_ms": None if low_age is None else round(low_age * 1000, 3),
            "source_topic": low_source,
            "data": low,
        },
        "sport": {
            "fresh": sport_age is not None and sport_age <= SPORT_MAX_AGE_S,
            "age_ms": None if sport_age is None else round(sport_age * 1000, 3),
            "source_topic": sport_source,
            "data": sport,
        },
    }


def _publish_loop() -> None:
    global _state_published
    period = 1.0 / STATE_PUBLISH_HZ
    while True:
        started = time.monotonic()
        snapshot = _canonical_state(started)
        low = snapshot["lowstate"]["data"]
        sport = snapshot["sport"]["data"]
        try:
            if snapshot["lowstate"]["fresh"]:
                log_time = int(low["received_at_ns"])
                _state_channel.log(snapshot, log_time=log_time)
                joints = low["joints"]
                _joints_channel.log(
                    JointStates(
                        timestamp=_timestamp(log_time),
                        joints=[
                            JointState(
                                name=name,
                                position=joints["position_rad"][index],
                                velocity=joints["velocity_rad_s"][index],
                                effort=joints["effort_nm"][index],
                            )
                            for index, name in enumerate(joints["name"])
                        ],
                    ),
                    log_time=log_time,
                )
                with _lock:
                    _state_published += 1
            if snapshot["sport"]["fresh"]:
                log_time = int(sport["received_at_ns"])
                position = sport["position_m"]
                quaternion = sport["imu"]["quaternion_wxyz"]
                rotation = Quaternion(
                    x=quaternion[1], y=quaternion[2], z=quaternion[3], w=quaternion[0]
                )
                translation = Vector3(x=position[0], y=position[1], z=position[2])
                pose = Pose(position=translation, orientation=rotation)
                _pose_channel.log(
                    PoseInFrame(
                        timestamp=_timestamp(log_time), frame_id="odom", pose=pose
                    ),
                    log_time=log_time,
                )
                _tf_channel.log(
                    FrameTransform(
                        timestamp=_timestamp(log_time),
                        parent_frame_id="odom",
                        child_frame_id="base_link",
                        translation=translation,
                        rotation=rotation,
                    ),
                    log_time=log_time,
                )
        except Exception:
            logger.exception("Foxglove state publish failed")
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, period - elapsed))


def _camera_loop() -> None:
    """Read the official Go2 VideoClient JPEG stream with restart-on-error."""

    minimum_period = 1.0 / CAMERA_MAX_FPS
    while True:
        try:
            from unitree_sdk2py.go2.video.video_client import VideoClient

            client = VideoClient()
            client.SetTimeout(CAMERA_TIMEOUT_S)
            client.Init()
            with _lock:
                _camera["restarts"] += 1
                _camera["status"] = "connected"
            consecutive_failures = 0
            while True:
                started = time.monotonic()
                try:
                    code, data = client.GetImageSample()
                    if code != 0:
                        raise RuntimeError(
                            f"VideoClient.GetImageSample returned code {code}"
                        )
                    jpeg = bytes(data)
                    if not 4 <= len(jpeg) <= CAMERA_MAX_JPEG_BYTES:
                        raise ValueError(f"camera JPEG has invalid size {len(jpeg)}")
                    if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(
                        b"\xff\xd9"
                    ):
                        raise ValueError("camera payload is not a complete JPEG")
                except Exception as error:
                    consecutive_failures += 1
                    rendered = f"{type(error).__name__}: {error}"
                    with _lock:
                        _rejected["camera"] += 1
                        _camera["failures"] += 1
                        _camera["status"] = "degraded"
                        _camera["last_error"] = rendered
                        _camera["last_error_at_ns"] = time.time_ns()
                        _last_error["camera"] = rendered
                    logger.warning(
                        "camera fetch failed (%d/3): %s", consecutive_failures, rendered
                    )
                    if consecutive_failures >= 3:
                        raise RuntimeError(
                            "three consecutive VideoClient failures; recreating client"
                        ) from error
                    time.sleep(0.2)
                    continue
                consecutive_failures = 0
                captured_at_ns = time.time_ns()
                _camera_channel.log(
                    CompressedImage(
                        timestamp=_timestamp(captured_at_ns),
                        frame_id="front_camera",
                        format="jpeg",
                        data=jpeg,
                    ),
                    log_time=captured_at_ns,
                )
                with _lock:
                    _camera["frames"] += 1
                    _camera["last_monotonic"] = time.monotonic()
                    _camera["last_at_ns"] = captured_at_ns
                    _camera["status"] = "streaming"
                elapsed = time.monotonic() - started
                time.sleep(max(0.0, minimum_period - elapsed))
        except Exception as error:  # noqa: BLE001 - recreate client after any stream failure
            with _lock:
                _camera["status"] = "retrying"
            logger.warning("camera client restarting: %s", error)
            time.sleep(2.0)


_NUMERIC = PackedElementFieldNumericType
_ROS_TO_FOX = {
    1: _NUMERIC.Int8,
    2: _NUMERIC.Uint8,
    3: _NUMERIC.Int16,
    4: _NUMERIC.Uint16,
    5: _NUMERIC.Int32,
    6: _NUMERIC.Uint32,
    7: _NUMERIC.Float32,
    8: _NUMERIC.Float64,
}


def _lidar_loop() -> None:
    while True:
        try:
            qos = Qos(Policy.Reliability.BestEffort, Policy.History.KeepLast(2))
            participant = DomainParticipant(DDS_DOMAIN)
            reader = DataReader(
                Subscriber(participant),
                Topic(participant, LIDAR_TOPIC, PointCloud2_, qos=qos),
                qos=qos,
            )
            with _lock:
                _lidar["status"] = "subscribed"
                _last_error["lidar"] = None
            while True:
                for message in reader.take_iter(timeout=1_000_000_000):
                    fields = [
                        PackedElementField(
                            name=field.name,
                            offset=field.offset,
                            type=_ROS_TO_FOX.get(field.datatype, _NUMERIC.Unknown),
                        )
                        for field in message.fields
                    ]
                    if int(message.point_step) <= 0 or not fields:
                        raise ValueError("LiDAR point cloud has no point layout")
                    captured_at_ns = time.time_ns()
                    _points_channel.log(
                        PointCloud(
                            timestamp=_timestamp(captured_at_ns),
                            frame_id=message.header.frame_id or "base_link",
                            pose=Pose(
                                position=Vector3(x=0, y=0, z=0),
                                orientation=Quaternion(x=0, y=0, z=0, w=1),
                            ),
                            point_stride=message.point_step,
                            fields=fields,
                            data=bytes(message.data),
                        ),
                        log_time=captured_at_ns,
                    )
                    with _lock:
                        _lidar["frames"] += 1
                        _lidar["last_monotonic"] = time.monotonic()
                        _lidar["status"] = "streaming"
                        _last_error["lidar"] = None
        except Exception as error:  # noqa: BLE001 - rebuild reader after DDS failure
            with _lock:
                _rejected["lidar"] += 1
                _lidar["status"] = "retrying"
            _record_error("lidar", error)
            time.sleep(2.0)


def _health_snapshot() -> dict[str, Any]:
    now = time.monotonic()
    with _lock:
        low_age = _age(_low_monotonic, now)
        sport_age = _age(_sport_monotonic, now)
        camera_age = _age(float(_camera["last_monotonic"]), now)
        lidar_age = _age(float(_lidar["last_monotonic"]), now)
        state = {
            "ok": low_age is not None and low_age <= STATE_MAX_AGE_S,
            "lowstate_age_s": None if low_age is None else round(low_age, 3),
            "sport_fresh": sport_age is not None and sport_age <= SPORT_MAX_AGE_S,
            "sport_age_s": None if sport_age is None else round(sport_age, 3),
            "lowstate_topic": _low_source,
            "sport_topic": _sport_source,
            "lowstate_received": _low_received,
            "sport_received": _sport_received,
            "published": _state_published,
        }
        camera = {
            "ok": camera_age is not None and camera_age <= CAMERA_MAX_AGE_S,
            "status": _camera["status"],
            "age_s": None if camera_age is None else round(camera_age, 3),
            "frames": _camera["frames"],
            "last_at_ns": _camera["last_at_ns"],
            "failures": _camera["failures"],
            "restarts": _camera["restarts"],
            "last_error": _camera["last_error"],
            "last_error_at_ns": _camera["last_error_at_ns"],
        }
        lidar = {
            "fresh": lidar_age is not None and lidar_age <= CAMERA_MAX_AGE_S,
            "status": _lidar["status"],
            "age_s": None if lidar_age is None else round(lidar_age, 3),
            "frames": _lidar["frames"],
        }
        dds = {
            "ok": _last_error["dds"] is None,
            "address": DDS_ADDRESS,
            "domain": DDS_DOMAIN,
        }
        errors = dict(_last_error)
        rejected = dict(_rejected)
    return {
        "ok": bool(dds["ok"] and state["ok"] and camera["ok"]),
        "dds": dds,
        "state": state,
        "camera": camera,
        "lidar": lidar,
        "rejected": rejected,
        "last_error": errors,
    }


def _health_publish_loop() -> None:
    while True:
        try:
            _health_channel.log(_health_snapshot(), log_time=time.time_ns())
        except Exception:
            logger.exception("Foxglove health publish failed")
        time.sleep(1.0)


api = FastAPI(title="Go2 canonical observability diagnostics")


@api.get("/healthz")
def healthz() -> dict[str, Any]:
    return _health_snapshot()


@api.get("/readyz")
def readyz() -> dict[str, Any]:
    return _health_snapshot()


def main() -> None:
    _start_unitree()
    for target, name in (
        (_publish_loop, "state-publisher"),
        (_camera_loop, "camera"),
        (_lidar_loop, "lidar"),
        (_health_publish_loop, "health-publisher"),
    ):
        threading.Thread(target=target, name=name, daemon=True).start()
    logger.info(
        "Go2 bridge ready: Foxglove %s:%d, diagnostics %s:%d",
        FOXGLOVE_BIND_HOST,
        FOXGLOVE_PORT,
        DIAG_BIND_HOST,
        DIAG_PORT,
    )
    uvicorn.run(api, host=DIAG_BIND_HOST, port=DIAG_PORT, log_level="warning")


if __name__ == "__main__":
    main()
