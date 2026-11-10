"""The arm controller has one fixed velocity clip, not a 20->30 rad/s ramp.

Unitree's speed_gradual_max() ramped the clip from 20 to 30 rad/s over 5 s.
Two thirds of full speed at t=0 protects nothing, and ARM_SLEW_RAD_S (2.5
rad/s, on the commanded targets) is the limit that actually binds. Unitree
removed the ramp upstream (xr_teleoperate 4cbf6e2, 2026-08-03) in favour of
set_arm_velocity_limit(); this mirrors that.
"""
import importlib
import math
import pathlib
import sys
import types

import pytest

REAL = pathlib.Path(__file__).parent.parent / "onboard"
# conftest imports retarget_g1 and expects a test module to have put bridge/ on the path.
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "teleop" / "bridge"))


def _g1_29_source():
    body = (REAL / "g1_arm.py").read_text()
    return body[body.index("class G1_29_ArmController"):body.index("class G1_23_ArmController")]


@pytest.fixture
def g1_arm(monkeypatch):
    """Import real/g1_arm.py with unitree_sdk2py stubbed out (import only)."""
    def mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, name, m)
    mod("unitree_sdk2py"); mod("unitree_sdk2py.core")
    mod("unitree_sdk2py.core.channel", ChannelPublisher=object, ChannelSubscriber=object,
        ChannelFactoryInitialize=lambda *a, **k: None)
    mod("unitree_sdk2py.idl"); mod("unitree_sdk2py.idl.unitree_hg"); mod("unitree_sdk2py.idl.unitree_hg.msg")
    mod("unitree_sdk2py.idl.unitree_hg.msg.dds_", LowCmd_=object, LowState_=object)
    mod("unitree_sdk2py.idl.unitree_go"); mod("unitree_sdk2py.idl.unitree_go.msg")
    mod("unitree_sdk2py.idl.unitree_go.msg.dds_", LowCmd_=object, LowState_=object)
    mod("unitree_sdk2py.idl.default", unitree_hg_msg_dds__LowCmd_=object, unitree_go_msg_dds__LowCmd_=object)
    mod("unitree_sdk2py.utils"); mod("unitree_sdk2py.utils.crc", CRC=object)
    monkeypatch.syspath_prepend(str(REAL))
    monkeypatch.delitem(sys.modules, "g1_arm", raising=False)
    return importlib.import_module("g1_arm")


def _bare(g1_arm):
    return g1_arm.G1_29_ArmController.__new__(g1_arm.G1_29_ArmController)


def test_the_ramp_is_gone_from_the_controller():
    """Match code, not the comments that explain what was removed and why."""
    src = _g1_29_source()
    for name in ("def speed_gradual_max", "def speed_instant_max", "self._speed_gradual_max",
                 "self._gradual_start_time", "20.0 + (10.0"):
        assert name not in src, f"the speed ramp is back: {name}"


def test_the_onboard_does_not_start_a_ramp():
    body = (REAL / "g1_onboard.py").read_text()
    assert ".speed_gradual_max(" not in body, "arm_on starts the controller ramp again"


def test_set_arm_velocity_limit_sets_a_float(g1_arm):
    arm = _bare(g1_arm)
    arm.set_arm_velocity_limit(12)
    assert arm.arm_velocity_limit == 12.0 and isinstance(arm.arm_velocity_limit, float)


def test_default_is_the_value_the_ramp_used_to_reach(g1_arm):
    arm = _bare(g1_arm)
    arm.set_arm_velocity_limit()
    assert arm.arm_velocity_limit == 50.0


@pytest.mark.parametrize("bad", [0.0, -1.0, math.nan, math.inf, -math.inf])
def test_set_arm_velocity_limit_rejects_a_useless_clip(g1_arm, bad):
    arm = _bare(g1_arm)
    with pytest.raises(ValueError):
        arm.set_arm_velocity_limit(bad)
