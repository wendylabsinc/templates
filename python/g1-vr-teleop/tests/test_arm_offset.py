"""Arming must not lurch.

The robot's arm is wherever control was taken. The operator's hand says
something else -- wrist targets are absolute angles about zero, the elbow comes
from a reach calibration that knows nothing about the robot's elbow, and the
shoulder is the same. Arming therefore commanded a pose unrelated to the one
the arm was holding, and the onboard slewed through the gap.

Measured on the first ARMED frame of a real episode: right_elbow stepped
53.8 deg. On a shoulder the same gap swings the whole arm.
"""
import math
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))
import sink_realg1 as sk  # noqa: E402


def _sink(state="ARMED", q=None):
    s = sk.RealG1Sink.__new__(sk.RealG1Sink)
    s._lock = threading.Lock()
    s.onboard_state = state
    s.onboard_q = q
    s._arm_offset = None
    s._warned_no_q = False
    return s


def _partial(**kw):
    return {f"left_{k}_joint": v for k, v in kw.items()}


def test_the_first_armed_command_is_the_pose_the_arm_is_already_in():
    q = [0.0] * 14
    q[sk.RealG1Sink._ARM_INDEX["left_elbow_joint"]] = 0.80      # robot's elbow
    s = _sink(q=q)
    out = s._offset_to_current_pose({"left_elbow_joint": -0.14})  # operator says
    assert out["left_elbow_joint"] == pytest.approx(0.80), \
        "arming commanded a pose the arm was not in"


def test_motion_after_arming_is_relative_to_that_pose():
    q = [0.0] * 14
    q[sk.RealG1Sink._ARM_INDEX["left_elbow_joint"]] = 0.80
    s = _sink(q=q)
    s._offset_to_current_pose({"left_elbow_joint": -0.14})       # captures
    moved = s._offset_to_current_pose({"left_elbow_joint": -0.14 + 0.30})
    assert moved["left_elbow_joint"] == pytest.approx(0.80 + 0.30), \
        "the operator's 0.30 rad move did not arrive as 0.30 rad"


def test_the_offset_is_captured_once_not_every_frame():
    """Re-capturing each frame would pin the arm and it would never move."""
    q = [0.0] * 14
    s = _sink(q=q)
    s._offset_to_current_pose({"left_elbow_joint": 0.5})
    out = s._offset_to_current_pose({"left_elbow_joint": 0.9})
    assert out["left_elbow_joint"] == pytest.approx(0.4), \
        "the offset was re-captured; the arm is pinned to its starting pose"


def test_leaving_armed_drops_the_offset():
    """Each arming must start from wherever the arm is THEN."""
    q = [0.0] * 14
    s = _sink(q=q)
    s._offset_to_current_pose({"left_elbow_joint": 0.5})
    assert s._arm_offset is not None
    s.onboard_state = "HOLDING"
    s._offset_to_current_pose({"left_elbow_joint": 0.5})
    assert s._arm_offset is None


def test_nothing_is_offset_while_not_armed():
    """HOLDING frames are not commands; leave them alone."""
    s = _sink(state="HOLDING", q=[0.0] * 14)
    out = s._offset_to_current_pose({"left_elbow_joint": 0.5})
    assert out["left_elbow_joint"] == pytest.approx(0.5)


def test_without_measured_angles_it_does_nothing_rather_than_guess():
    """An onboard too old to send them must degrade, not invent an offset."""
    s = _sink(q=None)
    out = s._offset_to_current_pose({"left_elbow_joint": 0.5})
    assert out["left_elbow_joint"] == pytest.approx(0.5)
    assert s._arm_offset is None


def test_every_driven_joint_is_offset_not_just_the_elbow():
    q = [0.1 * i for i in range(14)]
    s = _sink(q=q)
    names = sorted(sk.RealG1Sink._FOREARM_SET["left"]
                   | sk.RealG1Sink._SHOULDER_SET["left"])
    out = s._offset_to_current_pose({n: 0.0 for n in names})
    for n in names:
        assert out[n] == pytest.approx(q[sk.RealG1Sink._ARM_INDEX[n]]), n


def test_an_empty_partial_is_untouched():
    s = _sink(q=[0.0] * 14)
    assert s._offset_to_current_pose({}) == {}
    assert s._arm_offset is None, "an empty frame captured an offset"
