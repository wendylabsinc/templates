"""Pure normalization and source-selection helpers for Go2 observability."""

from __future__ import annotations

import ipaddress
import math
import socket
from typing import Any

JOINT_NAMES = (
    "front_right_hip_joint",
    "front_right_thigh_joint",
    "front_right_calf_joint",
    "front_left_hip_joint",
    "front_left_thigh_joint",
    "front_left_calf_joint",
    "rear_right_hip_joint",
    "rear_right_thigh_joint",
    "rear_right_calf_joint",
    "rear_left_hip_joint",
    "rear_left_thigh_joint",
    "rear_left_calf_joint",
)


def _finite(value: Any, field: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} is not finite")
    return number


def _vector(values: Any, length: int, field: str) -> list[float]:
    result = [_finite(value, f"{field}[{index}]") for index, value in enumerate(values)]
    if len(result) < length:
        raise ValueError(
            f"{field} has {len(result)} values; expected at least {length}"
        )
    return result[:length]


def _temperature(value: Any, field: str) -> float:
    if hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
        if len(value) == 0:
            raise ValueError(f"{field} is empty")
        value = value[0]
    return _finite(value, field)


def normalize_lowstate(
    message: Any, *, received_at_ns: int, sample_id: int
) -> dict[str, Any]:
    """Convert a Unitree Go2 LowState sample into a deterministic snapshot."""

    motors = list(message.motor_state)
    if len(motors) < len(JOINT_NAMES):
        raise ValueError(
            f"motor_state has {len(motors)} values; expected at least {len(JOINT_NAMES)}"
        )

    positions: list[float] = []
    velocities: list[float] = []
    efforts: list[float] = []
    temperatures: list[float] = []
    modes: list[int] = []
    for index, motor in enumerate(motors[: len(JOINT_NAMES)]):
        prefix = f"motor_state[{index}]"
        positions.append(_finite(motor.q, f"{prefix}.q"))
        velocities.append(_finite(motor.dq, f"{prefix}.dq"))
        efforts.append(_finite(motor.tau_est, f"{prefix}.tau_est"))
        temperatures.append(_temperature(motor.temperature, f"{prefix}.temperature"))
        modes.append(int(motor.mode))

    imu = message.imu_state
    foot_force = _vector(message.foot_force, 4, "foot_force")
    return {
        "sample_id": int(sample_id),
        "received_at_ns": int(received_at_ns),
        "tick": int(getattr(message, "tick", 0)),
        "imu": {
            "quaternion_wxyz": _vector(imu.quaternion, 4, "imu.quaternion"),
            "gyroscope_rad_s": _vector(imu.gyroscope, 3, "imu.gyroscope"),
            "accelerometer_m_s2": _vector(imu.accelerometer, 3, "imu.accelerometer"),
            "rpy_rad": _vector(imu.rpy, 3, "imu.rpy"),
            "temperature_c": _temperature(imu.temperature, "imu.temperature"),
        },
        "power": {
            "battery_percent": int(message.bms_state.soc),
            "voltage_v": _finite(message.power_v, "power_v"),
        },
        "foot_force_n": foot_force,
        "joints": {
            "name": list(JOINT_NAMES),
            "position_rad": positions,
            "velocity_rad_s": velocities,
            "effort_nm": efforts,
            "temperature_c": temperatures,
            "mode": modes,
        },
    }


def normalize_sportstate(
    message: Any, *, received_at_ns: int, sample_id: int
) -> dict[str, Any]:
    """Convert a Unitree Go2 SportModeState sample into a fixed pose snapshot."""

    imu = message.imu_state
    return {
        "sample_id": int(sample_id),
        "received_at_ns": int(received_at_ns),
        "position_m": _vector(message.position, 3, "position"),
        "velocity_m_s": _vector(message.velocity, 3, "velocity"),
        "body_height_m": _finite(getattr(message, "body_height", 0.0), "body_height"),
        "yaw_speed_rad_s": _finite(getattr(message, "yaw_speed", 0.0), "yaw_speed"),
        "gait_type": int(getattr(message, "gait_type", 0)),
        "foot_raise_height_m": _finite(
            getattr(message, "foot_raise_height", 0.0), "foot_raise_height"
        ),
        "imu": {
            "quaternion_wxyz": _vector(imu.quaternion, 4, "imu.quaternion"),
            "rpy_rad": _vector(imu.rpy, 3, "imu.rpy"),
        },
    }


class StickySourceSelector:
    """Use one topic until it is stale, avoiding duplicate firmware aliases."""

    def __init__(self, stale_after_s: float) -> None:
        if not math.isfinite(stale_after_s) or stale_after_s <= 0:
            raise ValueError("stale_after_s must be positive")
        self.stale_after_s = stale_after_s
        self.active: str | None = None
        self.last_seen: dict[str, float] = {}

    def accept(self, source: str, now: float) -> bool:
        previous_active_seen = self.last_seen.get(self.active or "", 0.0)
        self.last_seen[source] = now
        if self.active is None:
            self.active = source
            return True
        if source == self.active:
            return True
        if now - previous_active_seen > self.stale_after_s:
            self.active = source
            return True
        return False


def resolve_dds_address(robot_ip: str, override: str = "") -> str:
    """Resolve the local IPv4 source address used to reach the Go2 controller."""

    candidate = override.strip()
    if not candidate:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((robot_ip, 1))
            candidate = sock.getsockname()[0]
        finally:
            sock.close()
    address = ipaddress.ip_address(candidate)
    if address.version != 4 or address.is_unspecified or address.is_loopback:
        raise ValueError(f"invalid DDS source address: {candidate!r}")
    robot_address = ipaddress.IPv4Address(robot_ip)
    robot_network = ipaddress.IPv4Network(f"{robot_address}/24", strict=False)
    if not override.strip() and address not in robot_network:
        raise ValueError(
            f"route to Go2 {robot_address} selected {address}, outside {robot_network}; "
            "connect this device to the robot LAN or set GO2_DDS_ADDRESS explicitly"
        )
    return str(address)


def cyclonedds_config(address: str) -> str:
    """Return a CycloneDDS config pinned to one exact, validated IPv4 address."""

    validated = str(ipaddress.IPv4Address(address))
    return (
        '<CycloneDDS><Domain Id="any"><General><Interfaces>'
        f'<NetworkInterface address="{validated}" priority="default" multicast="default"/>'
        "</Interfaces></General></Domain></CycloneDDS>"
    )
