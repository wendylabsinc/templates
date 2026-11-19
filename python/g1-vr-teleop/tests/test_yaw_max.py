"""How far the shoulder may twist, as a dial rather than a switch.

Measured on this robot, right arm, with and without the yaw:

            elbow follow   shoulder pitch   shoulder roll
  yaw ON        116 %           75 %            84 %
  yaw OFF        50 %           45 %            42 %

It roughly doubles the arm. But the operator's words with it on were "moves
wrong -- goes somewhere I didn't point", and that was a real observation: the
HAND lands correctly while the upper arm rotates into a posture nobody asked
for. Between "twice the reach, looks wrong" and "half the reach, looks right"
there is a number, and nobody here can derive it.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "teleop" / "bridge"))
import retarget_g1 as rg                                        # noqa: E402

R2D = 57.29577951308232


def _r():
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    r.shoulder_yaw = True
    r.YAW_SEARCH_MAX = rg.G1Retargeter.YAW_SEARCH_MAX
    return r


def test_the_default_is_the_seventy_degrees_the_measurements_used():
    assert rg.G1Retargeter.YAW_SEARCH_MAX * R2D == pytest.approx(70.0, abs=0.5)


def test_it_can_be_dialled_down_without_being_turned_off():
    r = _r()
    assert r.set_yaw_max(30) == 30.0
    assert r.YAW_SEARCH_MAX * R2D == pytest.approx(30.0, abs=0.01)
    assert r.shoulder_yaw is True, "30 deg of twist is still a twist"


def test_zero_turns_the_search_off_rather_than_leaving_an_empty_one():
    """At 0 the grid has no points and attempt(0.0) is all there is -- which is
    the two-joint shoulder. Saying so beats a search that cannot search."""
    r = _r()
    assert r.set_yaw_max(0) == 0.0
    assert r.shoulder_yaw is False


def test_turning_it_back_up_re_enables_it():
    r = _r()
    r.set_yaw_max(0)
    r.set_yaw_max(45)
    assert r.shoulder_yaw is True and r.YAW_SEARCH_MAX * R2D == pytest.approx(45, abs=0.01)


def test_it_refuses_nonsense_and_anything_past_the_joint():
    r = _r()
    for bad in (-1, 91, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            r.set_yaw_max(bad)


def test_it_is_per_instance_not_a_class_wide_change():
    """Two retargeters in one process -- a replay beside the live one -- must
    not share a dial the operator moved."""
    a, b = _r(), _r()
    a.set_yaw_max(20)
    assert b.YAW_SEARCH_MAX * R2D == pytest.approx(70.0, abs=0.5)
    assert rg.G1Retargeter.YAW_SEARCH_MAX * R2D == pytest.approx(70.0, abs=0.5)
