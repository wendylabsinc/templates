"""Finger calibration: stretch the operator's real range onto the joint's.

Measured on hardware: DexPilot hits the curl limit exactly and a flat open hand
only reaches about halfway -- left middle_0 stopped at -0.879 of a [-1.571, 0]
range. The fingers never opened past ~50%.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))


class Cal:
    """The calibration maths, lifted from G1Retargeter (which cannot be
    imported without pinocchio + dex_retargeting)."""
    from_module = True


def _apply(joint, v, open_v, closed_v, ranges, min_span=0.15):
    lo, hi = ranges[joint]
    closed_end, open_end = (hi, lo) if abs(closed_v - hi) < abs(closed_v - lo) else (lo, hi)
    span = closed_v - open_v
    if abs(span) < min_span:
        return v
    t = min(1.0, max(0.0, (v - open_v) / span))
    return open_end + t * (closed_end - open_end)


R = {"left_hand_middle_0_joint": (-1.57079632, 0.0),
     "right_hand_index_0_joint": (0.0, 1.57079632)}


def test_the_measured_shortfall_becomes_full_range():
    """The real numbers off the robot: open reached -0.879, closed -1.571."""
    j = "left_hand_middle_0_joint"
    assert _apply(j, -0.879, -0.879, -1.571, R) == pytest.approx(0.0, abs=1e-6), \
        "a fully open hand must command the fully open joint"
    assert _apply(j, -1.571, -0.879, -1.571, R) == pytest.approx(-1.57079632, abs=1e-6), \
        "a fist must still command the full curl"


def test_it_works_for_the_mirrored_hand_too():
    j = "right_hand_index_0_joint"
    assert _apply(j, 0.959, 0.959, 1.572, R) == pytest.approx(0.0, abs=1e-6)
    assert _apply(j, 1.572, 0.959, 1.572, R) == pytest.approx(1.57079632, abs=1e-6)


def test_posing_beyond_the_recorded_range_is_clamped():
    """A hand opened wider than during calibration must not command past the
    joint limit."""
    j = "left_hand_middle_0_joint"
    v = _apply(j, -0.5, -0.879, -1.571, R)      # beyond the recorded open
    lo, hi = R[j]
    assert lo - 1e-9 <= v <= hi + 1e-9
    v = _apply(j, -2.0, -0.879, -1.571, R)      # beyond the recorded closed
    assert lo - 1e-9 <= v <= hi + 1e-9


def test_two_poses_that_barely_differ_are_left_alone():
    """Stretching a 0.05 rad difference to full scale would turn tracking noise
    into full-scale finger motion."""
    j = "left_hand_middle_0_joint"
    assert _apply(j, -1.0, -1.0, -1.05, R) == -1.0, "amplified an unusable span"


def test_the_midpoint_maps_to_the_midpoint():
    j = "left_hand_middle_0_joint"
    v = _apply(j, -1.225, -0.879, -1.571, R)
    assert v == pytest.approx(-1.57079632 / 2, abs=0.02)


def test_the_module_has_the_calibration_api():
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "teleop", "bridge", "retarget_g1.py")).read()
    for fn in ("def cal_start", "def cal_mark", "def cal_finish", "def cal_clear",
               "def _apply_cal"):
        assert fn in src, f"{fn} missing"
    assert "CAL_MIN_SPAN" in src
