"""Does the robot move the way the operator moved?

Today's bug was in the TRANSLATION (_PELVIS_TO_HEAD). Nobody has ever checked
the ROTATION, _R_XR_TO_ROBOT. A wrong axis permutation or sign there produces a
robot that tracks your hands beautifully in the wrong direction -- you reach
forward, it reaches sideways -- and it is indistinguishable from today's failure
from the outside, and invisible to every test we have: targets would still be
finite, inside joint limits, inside the workspace, and changing as you move.

These tests assert direction, not motion. "Did it move" is what let an 0.84 m
aiming error pass for weeks.

The robot's own frame convention is DERIVED from the URDF rather than assumed:
the left shoulder's +y and the shoulders' +z fix which way is left and up.
"""
import os
import sys
import xml.etree.ElementTree as ET

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))

_R_XR_TO_ROBOT = np.array([[0.0, 0.0, -1.0], [-1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
_PELVIS_TO_HEAD = np.array([0.054, 0.0, 0.474])
_REACH_SCALE = 0.74
_URDF = os.path.join(os.path.dirname(__file__), "..", "teleop", "assets", "g1",
                     "g1_body29_hand14.urdf")


def target(pos_head):
    return _R_XR_TO_ROBOT @ (np.asarray(pos_head, float) * _REACH_SCALE) + _PELVIS_TO_HEAD


# --------------------------------------------------------------------------- #
# the robot's frame convention, from the model
# --------------------------------------------------------------------------- #
def _joint_origin(name):
    root = ET.parse(_URDF).getroot()
    for j in root.findall("joint"):
        if j.get("name") == name:
            o = j.find("origin")
            return np.array([float(v) for v in o.get("xyz").split()])
    raise AssertionError(f"{name} not in the URDF")


def test_the_urdf_says_plus_y_is_the_robots_left():
    """Everything below depends on this, so read it rather than assume it."""
    left = _joint_origin("left_shoulder_pitch_joint")
    right = _joint_origin("right_shoulder_pitch_joint")
    assert left[1] > 0 > right[1], (
        f"left shoulder y={left[1]}, right y={right[1]} — +y is not left")


def test_the_urdf_says_plus_z_is_up():
    """Shoulders sit above the pelvis; if they did not, +z is not up."""
    assert _joint_origin("left_shoulder_pitch_joint")[2] > 0.1


# --------------------------------------------------------------------------- #
# direction: operator moves one way, the target must move the same way
#
# WebXR head frame: +X right, +Y up, -Z forward.
# G1 pelvis frame:  +x forward, +y left, +z up (asserted above).
# --------------------------------------------------------------------------- #
ORIGIN = (-0.20, -0.35, -0.35)          # a plausible hand pose to perturb from
STEP = 0.10

MOVES = {
    # operator moves...          delta in XR             expected robot axis, sign
    "forward":                  ((0.0, 0.0, -STEP),      0, +1),
    "backward":                 ((0.0, 0.0, +STEP),      0, -1),
    "to their left":            ((-STEP, 0.0, 0.0),      1, +1),
    "to their right":           ((+STEP, 0.0, 0.0),      1, -1),
    "up":                       ((0.0, +STEP, 0.0),      2, +1),
    "down":                     ((0.0, -STEP, 0.0),      2, -1),
}


@pytest.mark.parametrize("name,delta,axis,sign",
                         [(k, *v) for k, v in MOVES.items()])
def test_the_robot_moves_the_same_way_the_operator_did(name, delta, axis, sign):
    base = target(ORIGIN)
    moved = target(np.array(ORIGIN) + np.array(delta))
    d = moved - base
    got = d[axis]
    assert np.sign(got) == sign, (
        f"operator moved {name}; robot target moved {d.round(4)} — "
        f"axis {'xyz'[axis]} went {got:+.4f}, expected sign {sign:+d}")


@pytest.mark.parametrize("name,delta,axis,sign",
                         [(k, *v) for k, v in MOVES.items()])
def test_no_leakage_into_the_other_axes(name, delta, axis, sign):
    """A pure operator movement must not produce robot motion on other axes.

    Cross-axis leakage is how a permuted rotation still passes a
    "did it move in roughly the right direction" check.
    """
    d = target(np.array(ORIGIN) + np.array(delta)) - target(ORIGIN)
    for other in range(3):
        if other == axis:
            continue
        assert abs(d[other]) < 1e-9, (
            f"operator moved {name}; robot target also moved "
            f"{d[other]:+.4f} on {'xyz'[other]}")


def test_movement_is_scaled_down_not_up():
    """The robot's arm is shorter than the operator's."""
    d = np.linalg.norm(target(np.array(ORIGIN) + np.array((0.0, 0.0, -0.20)))
                       - target(ORIGIN))
    assert d == pytest.approx(0.20 * _REACH_SCALE, rel=1e-6)
    assert d < 0.20, "operator motion was magnified onto a shorter arm"


def test_the_rotation_is_a_proper_rotation():
    """Orthonormal, right-handed. A typo here mirrors or skews the mapping."""
    R = _R_XR_TO_ROBOT
    assert np.allclose(R @ R.T, np.eye(3)), "not orthonormal"
    assert np.linalg.det(R) == pytest.approx(1.0), (
        f"det={np.linalg.det(R):.3f} — a reflection, not a rotation; "
        "the robot would mirror the operator")


def test_the_module_still_uses_this_rotation():
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "teleop", "bridge", "retarget_g1.py")).read()
    for row in ("[0.0, 0.0, -1.0]", "[-1.0, 0.0, 0.0]", "[0.0, 1.0, 0.0]"):
        assert row in src, f"_R_XR_TO_ROBOT changed; re-derive the expectations ({row})"
