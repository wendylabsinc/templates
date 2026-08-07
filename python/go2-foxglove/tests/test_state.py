from types import SimpleNamespace

import pytest
from state import (
    JOINT_NAMES,
    StickySourceSelector,
    cyclonedds_config,
    normalize_lowstate,
    normalize_sportstate,
    resolve_dds_address,
)


def _motor(index: int) -> SimpleNamespace:
    return SimpleNamespace(
        q=index + 0.1,
        dq=index + 0.2,
        tau_est=index + 0.3,
        temperature=30 + index,
        mode=1,
    )


def _imu() -> SimpleNamespace:
    return SimpleNamespace(
        quaternion=[1.0, 0.0, 0.0, 0.0],
        gyroscope=[0.1, 0.2, 0.3],
        accelerometer=[0.0, 0.0, 9.81],
        rpy=[0.01, 0.02, 0.03],
        temperature=42,
    )


def test_lowstate_is_fixed_width_and_deterministic() -> None:
    message = SimpleNamespace(
        motor_state=[_motor(index) for index in range(20)],
        imu_state=_imu(),
        foot_force=[1, 2, 3, 4],
        bms_state=SimpleNamespace(soc=82),
        power_v=29.4,
        tick=123,
    )
    result = normalize_lowstate(message, received_at_ns=99, sample_id=7)

    assert result["sample_id"] == 7
    assert result["joints"]["name"] == list(JOINT_NAMES)
    assert len(result["joints"]["position_rad"]) == 12
    assert result["joints"]["position_rad"][-1] == pytest.approx(11.1)
    assert result["foot_force_n"] == [1.0, 2.0, 3.0, 4.0]


def test_lowstate_rejects_partial_or_non_finite_samples() -> None:
    message = SimpleNamespace(
        motor_state=[_motor(index) for index in range(11)],
        imu_state=_imu(),
        foot_force=[1, 2, 3, 4],
        bms_state=SimpleNamespace(soc=82),
        power_v=29.4,
    )
    with pytest.raises(ValueError, match="expected at least 12"):
        normalize_lowstate(message, received_at_ns=99, sample_id=7)

    message.motor_state.append(_motor(11))
    message.motor_state[4].q = float("nan")
    with pytest.raises(ValueError, match="not finite"):
        normalize_lowstate(message, received_at_ns=99, sample_id=7)


def test_sportstate_uses_wxyz_and_fixed_vectors() -> None:
    result = normalize_sportstate(
        SimpleNamespace(
            position=[1, 2, 3],
            velocity=[4, 5, 6],
            body_height=0.3,
            yaw_speed=0.4,
            gait_type=2,
            foot_raise_height=0.1,
            imu_state=_imu(),
        ),
        received_at_ns=100,
        sample_id=9,
    )
    assert result["position_m"] == [1.0, 2.0, 3.0]
    assert result["imu"]["quaternion_wxyz"] == [1.0, 0.0, 0.0, 0.0]


def test_source_selector_sticks_until_active_source_is_stale() -> None:
    selector = StickySourceSelector(stale_after_s=0.5)
    assert selector.accept("rt/lf/lowstate", 1.0)
    assert not selector.accept("rt/lowstate", 1.1)
    assert selector.accept("rt/lf/lowstate", 1.2)
    assert not selector.accept("rt/lowstate", 1.6)
    assert selector.accept("rt/lowstate", 1.8)
    assert selector.active == "rt/lowstate"


def test_cyclonedds_config_accepts_only_ipv4() -> None:
    assert 'address="192.168.123.18"' in cyclonedds_config("192.168.123.18")
    with pytest.raises(ValueError):
        cyclonedds_config("not-an-address")


def test_explicit_dds_address_is_validated_without_route_lookup() -> None:
    assert resolve_dds_address("192.168.123.161", "192.168.123.18") == "192.168.123.18"
    with pytest.raises(ValueError, match="invalid DDS source"):
        resolve_dds_address("192.168.123.161", "127.0.0.1")
