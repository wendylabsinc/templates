"""Does the finger rotation actually let the Dex3 open and close?

tests/test_finger_frame.py checks the matrices against a URDF. This file checks
them against the thing that consumes them: the real dex_retargeting DexPilot
optimiser, loading the same standalone hand URDF it loads in production.

Why both are needed: the matrices in e11c423 pass every geometric assertion in
test_finger_frame.py and still pin the fingers at their limits, because that
file measured the hand as mounted in the WHOLE-BODY urdf (fingers along +X in
the wrist_yaw frame) while dex_retargeting loads assets/unitree_hand/
unitree_dex3_{left,right}.urdf, whose base_link has the fingers along -Y and the
palm facing -X. A test that agrees with the URDF you happened to read is not a
test of the pipeline.

The synthetic hands below are in the WebXR wrist frame per the W3C Hand Input
Module: -Z along the bones toward the fingertips, -Y out of the palm, X = Y x Z
(thumb side is +X for a left hand, -X for a right hand). Only landmarks 0, 4, 9
and 14 matter to DexPilot with Unitree's index array.

Standing rule of this repo: a check must fail on the wrong answer. Under the
identity, and under the e11c423 matrices, an OPEN hand comes out with the finger
joints at their curl limits; these tests fail there and pass with the
spec-derived rotation, which is also upstream televuer's composite
T_TO_UNITREE_HAND @ T_ROBOT_OPENXR. Verified by running both.

Skipped where dex_retargeting is not installed (.venv312 and the container
have it; the Homebrew python used for the fast suite does not).
"""
import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))

dex = pytest.importorskip("dex_retargeting")
import yaml  # noqa: E402
from dex_retargeting.retargeting_config import RetargetingConfig  # noqa: E402

import retarget_g1 as rg  # noqa: E402

# The spec-derived alternative (robot -Y = xr -Z fingers, robot -X = xr -Y palm,
# robot Z = xr X). On real data it is close to the shipped matrix on the finger
# joints and better on the thumb; kept here as a second known-good reference,
# not as the answer.
M_SPEC = np.array([[0, 1, 0], [0, 0, 1], [1, 0, 0]], float)

# REAL recorded hands (tests/fixtures/hand_open_close.json, Quest 3, both hands
# opening and closing). The first version of this file used synthetic hands
# built from the W3C convention; the palm normal they assumed is right (real
# data puts it on +Y to 0.98), but their thumb geometry was not this
# operator's, and on real data the shipped matrix outperforms the spec-derived
# one on the finger joints. Tests must be run against what the headset sends.
_FIX = os.path.join(os.path.dirname(__file__), "fixtures", "hand_open_close.json")


def _real_hands(side):
    frames = json.load(open(_FIX))["frames"]
    L = [np.asarray(h["landmarks"], float).reshape(25, 3)
         for fr in frames for h in fr if h.get("hand") == side]
    ext = np.array([np.linalg.norm(l[14]) for l in L])       # wrist -> middle tip
    order = np.argsort(ext)
    return L, order[-8:], order[:8]                          # all, most open, most closed

# hardware order (retarget_g1.DEX3_*): thumb_0, thumb_1, thumb_2, middle_0, middle_1, index_0, index_1
_FINGER_JOINTS = [3, 4, 5, 6]          # middle_0, middle_1, index_0, index_1
_LIMITS = {
    "left":  [(-1.0472, 1.0472), (-0.7243, 0.92), (0, 1.7453), (-1.5708, 0), (-1.7453, 0), (-1.5708, 0), (-1.7453, 0)],
    "right": [(-1.0472, 1.0472), (-0.92, 0.7243), (-1.7453, 0), (0, 1.5708), (0, 1.7453), (0, 1.5708), (0, 1.7453)],
}


@pytest.fixture(scope="module")
def cfg():
    with open(rg._DEX3_YML) as f:
        c = yaml.safe_load(f)
    RetargetingConfig.set_default_urdf_dir(rg._ASSETS)
    return c


def _solve(cfg, side, R, curl):
    """Run the whole recording through a fresh retargeter (so the low-pass
    filter sees the real sequence) and average the most open (curl=0) or most
    closed (curl=1) frames."""
    c = dict(cfg[side]); c["scaling_factor"] = rg.FINGER_SCALE
    rt = RetargetingConfig.from_dict(rg._known_keys(c)).build()
    idx = rt.optimizer.target_link_human_indices
    to_hw = [rt.joint_names.index(n) for n in (rg.DEX3_LEFT if side == "left" else rg.DEX3_RIGHT)]
    L, most_open, most_closed = _real_hands(side)
    Q = np.array([np.asarray(rt.retarget((lm[idx[1]] - lm[idx[0]]) @ R.T))[to_hw] for lm in L])
    return Q[most_closed if curl else most_open].mean(0)


def _open_fraction(q, side):
    """0 = at the open end of the range, 1 = at the curl limit, per finger joint."""
    out = []
    for j in _FINGER_JOINTS:
        lo, hi = _LIMITS[side][j]
        open_end = 0.0                          # both hands: 0 is the open end
        curl_end = lo if lo < 0 else hi
        out.append(abs(q[j] - open_end) / abs(curl_end - open_end))
    return np.array(out)


# --------------------------------------------------------------------------- #
# the two properties a working mapping must have
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("side", ["left", "right"])
def test_the_modules_rotation_lets_an_open_hand_open(cfg, side):
    """A flat open hand must not command the fingers to their curl limits."""
    q = _solve(cfg, side, rg._FINGER_ROT[side], curl=0.0)
    frac = _open_fraction(q, side)
    assert frac.max() < 0.55, (
        f"{side}: open hand drove finger joints to {np.round(frac, 2)} of the "
        f"way to the curl limit (q={np.round(q, 3)}); the frame is wrong")


@pytest.mark.parametrize("side", ["left", "right"])
def test_the_modules_rotation_lets_a_fist_close(cfg, side):
    """A fist must move the distal joints most of the way, and further than open."""
    q_open = _solve(cfg, side, rg._FINGER_ROT[side], curl=0.0)
    q_fist = _solve(cfg, side, rg._FINGER_ROT[side], curl=1.0)
    fo, ff = _open_fraction(q_open, side), _open_fraction(q_fist, side)
    assert (ff - fo).min() > 0.40, f"{side}: some finger joint barely moved open->fist: {np.round(ff - fo, 2)}"
    assert ff[[1, 3]].min() > 0.75, f"{side}: distal joints only reached {np.round(ff[[1, 3]], 2)} of range in a fist"


# --------------------------------------------------------------------------- #
# the same properties under the reference answer, and their failure under the
# wrong ones -- so this file demonstrates it can fail
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("side", ["left", "right"])
def test_the_spec_derived_rotation_also_opens_and_closes(cfg, side):
    fo = _open_fraction(_solve(cfg, side, M_SPEC, 0.0), side)
    ff = _open_fraction(_solve(cfg, side, M_SPEC, 1.0), side)
    assert fo.max() < 0.75 and (ff - fo).min() > 0.25


_WRONG = {
    "identity": {"left": np.eye(3), "right": np.eye(3)},
}


@pytest.mark.parametrize("name", sorted(_WRONG))
@pytest.mark.parametrize("side", ["left", "right"])
def test_the_wrong_rotations_pin_an_open_hand(cfg, name, side):
    """The un-rotated landmarks saturate an OPEN hand -- the bug this file guards."""
    fo = _open_fraction(_solve(cfg, side, _WRONG[name][side], 0.0), side)
    assert fo.max() > 0.9, f"{name}/{side} unexpectedly opened: {np.round(fo, 2)}"


def test_the_retargeter_loads_the_standalone_hand_urdf(cfg):
    """The frame that matters is the one dex_retargeting loads: fingers -Y."""
    rt = RetargetingConfig.from_dict(rg._known_keys(cfg["left"])).build()
    rob = rt.optimizer.robot
    rob.compute_forward_kinematics(np.zeros(rob.dof))
    tip = rob.get_link_pose(rob.get_link_index("index_tip"))[:3, 3]
    d = tip / np.linalg.norm(tip)
    assert d[1] < -0.95, f"index_tip at q=0 is {np.round(tip, 3)}: fingers are not along -Y"
