"""Live physical caps remain bounded, persistent and distinct from replay."""
import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from tests.test_waist_yaw import fake_sdk  # noqa: F401


@pytest.fixture
def cmd(tmp_path, monkeypatch):
    import g1_onboard as ob
    monkeypatch.setattr(ob, "MOTION_LIMITS_PATH", str(tmp_path / "motion-limits.json"))
    return ob.Commander()


def test_caps_persist_and_drive_upstream_slew(cmd, monkeypatch):
    import g1_onboard as ob
    limits = {"max_velocity_rad_s": 4., "max_acceleration_rad_s2": 20.}
    assert cmd.set_motion_limits(limits) == "motion limits applied and saved"
    assert ob.Commander().motion_limits == limits
    cmd._last_cmd = np.zeros(14)
    cmd._last_cmd_t = 10.
    cmd._slew(None, np.ones(14), 10.01)
    assert cmd._last_cmd == pytest.approx(np.full(14, .04))
    assert json.loads(open(ob.MOTION_LIMITS_PATH).read()) == limits


@pytest.mark.parametrize("limits", [None, {}, {"max_velocity_rad_s": 51, "max_acceleration_rad_s2": 16},
    {"max_velocity_rad_s": 3.5, "max_acceleration_rad_s2": float("nan")},
    {"max_velocity_rad_s": True, "max_acceleration_rad_s2": 16}])
def test_bad_caps_do_not_change_configuration(cmd, limits):
    before = dict(cmd.motion_limits)
    assert cmd.set_motion_limits(limits).startswith("refused")
    assert cmd.motion_limits == before


@pytest.mark.parametrize("state", ["ARMED", "LIMP", "HOMING"])
def test_caps_refuse_moving_or_limp_states(cmd, state):
    cmd.state = state
    assert cmd.set_motion_limits({"max_velocity_rad_s": 4, "max_acceleration_rad_s2": 20}).startswith("refused")
    assert cmd.motion_limits["max_velocity_rad_s"] == 35.0


def test_hold_preserves_live_caps_without_momentum(fake_sdk):
    from g1_arm import G1_29_ArmController
    from arm_reference import ArmReference
    ctl = G1_29_ArmController.__new__(G1_29_ArmController)
    ctl.ctrl_lock = threading.Lock()
    ctl.arm_reference = ArmReference(np.zeros(14))
    ctl.get_current_dual_arm_q = lambda: np.full(14, .1)
    ctl.set_arm_motion_limits({"max_velocity_rad_s": 4, "max_acceleration_rad_s2": 20})
    ctl.arm_reference.velocity[:] = 1
    ctl.hold_arm_measured()
    assert ctl.arm_reference.max_velocity == 4
    assert ctl.arm_reference.max_acceleration == 20
    assert np.all(ctl.arm_reference.velocity == 0)
    first_tick = ctl.arm_reference.advance(np.ones(14), np.full(14, .1), .01)
    assert first_tick == pytest.approx(np.full(14, .102))


def test_holding_updates_motor_and_write_failure_changes_nothing(cmd, monkeypatch):
    import g1_onboard as ob
    writes = []
    cmd.arm = SimpleNamespace(set_arm_motion_limits=lambda limits: writes.append(dict(limits)))
    cmd.state = "HOLDING"
    limits = {"max_velocity_rad_s": 4, "max_acceleration_rad_s2": 20}
    assert cmd.set_motion_limits(limits) == "motion limits applied and saved"
    assert writes == [limits]
    monkeypatch.setattr(ob, "MOTION_LIMITS_PATH", "/dev/null/unwritable.json")
    assert cmd.set_motion_limits({**limits, "max_velocity_rad_s": 5}).startswith("refused")
    assert writes == [limits]
    assert cmd.motion_limits == limits


def test_homing_and_invalid_saved_caps(cmd, monkeypatch):
    import g1_onboard as ob
    cmd.state = "HOLDING"
    cmd.homing = True
    assert cmd.set_motion_limits({"max_velocity_rad_s": 4, "max_acceleration_rad_s2": 20}).startswith("refused")
    with open(ob.MOTION_LIMITS_PATH, "w") as f:
        f.write('{"max_velocity_rad_s":999,"max_acceleration_rad_s2":20}')
    assert ob.Commander().motion_limits == {"max_velocity_rad_s": 35.0, "max_acceleration_rad_s2": 16.}


def test_ten_x_speed_profile_and_ceiling_are_validated():
    from arm_reference import ArmReference, validate_motion_limits
    assert ArmReference.max_velocity == 35.0
    assert ArmReference.frequency == 40.0
    assert validate_motion_limits({"max_velocity_rad_s": 50,
                                   "max_acceleration_rad_s2": 16})["max_velocity_rad_s"] == 50.0
    with pytest.raises(ValueError, match="between 0.25 and 50"):
        validate_motion_limits({"max_velocity_rad_s": 50.01,
                                "max_acceleration_rad_s2": 16})

