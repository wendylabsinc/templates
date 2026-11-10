"""A released trigger must OPEN the hand, not half-open it.

The operator, on controllers: "the thumb is so close to the body and cant
extend as is the default and stays close". It was not a gain, a calibration
or the thermal guard -- it was the open pose itself.

`grip_q` built its open pose as zero clamped into the joint's range. For six
of the seven Dex3 joints that is the open end, because their range stops at
zero. thumb_1 is the exception: it straddles zero (left -0.724..+0.920, right
-0.920..+0.724), so zero is the MIDDLE of its travel and the extending half
was never commanded. Measured before the fix: every other joint used 100 % of
its range and thumb_1 used 56 %.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_p = str(Path(__file__).resolve().parent.parent / "teleop" / "bridge")
if _p not in sys.path:
    sys.path.insert(0, _p)

import retarget_g1 as rg  # noqa: E402

SIDES = ("left", "right")


def _names(side):
    return rg.DEX3_LEFT if side == "left" else rg.DEX3_RIGHT


@pytest.mark.parametrize("side", SIDES)
def test_every_finger_but_the_thumb_uses_its_whole_range(side):
    """The grip is the only thing driving these; half a range is half a grasp.

    thumb_1 is excluded on purpose: how far it may open is a contact
    question, not a range question -- see the dial below.
    """
    opened, closed = rg.grip_q(side, 0.0), rg.grip_q(side, 1.0)
    for n, a, b in zip(_names(side), opened, closed):
        if n.endswith(("thumb_0_joint", "thumb_1_joint")):
            continue
        lo, hi = rg.DEX3_RANGE[n]
        assert {round(a, 6), round(b, 6)} == {round(lo, 6), round(hi, 6)}, (
            f"{n} goes {a:+.3f}..{b:+.3f} inside [{lo:+.3f}, {hi:+.3f}] -- "
            "the open pose is not an end of the range")


@pytest.mark.parametrize("side", SIDES)
def test_the_thumb_opens_past_neutral_without_reaching_its_limit(side):
    """The default must open the thumb, and must NOT drive it to the stop.

    Both halves matter. Zero is the half-open pose the operator complained
    about; the URDF limit is where the motor stops, not where the thumb stops
    hitting the hand -- "if it opens all the way then it hits the body". So
    the default sits between them and the number is found on the robot.
    """
    n = f"{side}_hand_thumb_1_joint"
    i = _names(side).index(n)
    lo, hi = rg.DEX3_RANGE[n]
    released = rg.grip_q(side, 0.0)[i]
    squeezed = rg.grip_q(side, 1.0)[i]
    extended = lo if squeezed == pytest.approx(hi) else hi
    assert abs(released) > 0.1, "0.0 is the middle of this joint, not open"
    assert 0.0 < released / extended < 1.0, (
        f"{n} released to {released:+.3f}: it must open TOWARD {extended:+.3f} "
        "and stop short of it")


@pytest.mark.parametrize("side", SIDES)
def test_the_thumb_dial_spans_neutral_to_the_limit_and_refuses_more(side):
    """0 and 1 are both reachable; nothing may ask for past the stop."""
    n = f"{side}_hand_thumb_1_joint"
    i = _names(side).index(n)
    lo, hi = rg.DEX3_RANGE[n]
    squeezed = rg.grip_q(side, 1.0)[i]
    extended = lo if squeezed == pytest.approx(hi) else hi
    was = rg.THUMB_OPEN
    try:
        rg.set_thumb_open(0.0)
        assert rg.grip_q(side, 0.0)[i] == pytest.approx(0.0)
        rg.set_thumb_open(1.0)
        assert rg.grip_q(side, 0.0)[i] == pytest.approx(extended)
        for bad in (-0.1, 1.1, float("nan"), float("inf")):
            with pytest.raises(ValueError):
                rg.set_thumb_open(bad)
    finally:
        rg.set_thumb_open(was)


@pytest.mark.parametrize("side", SIDES)
def test_thumb_0_stays_neutral_and_is_not_swept_to_a_limit(side):
    """Opposition is a separate question (GRIP_THUMB0) and still unanswered.

    The open-pose fix must not quietly answer it by parking this joint on a
    limit: which sign opposes is a fact about this hand nobody has measured.
    """
    n = f"{side}_hand_thumb_0_joint"
    i = _names(side).index(n)
    assert rg.GRIP_THUMB0 == 0.0, "the dial's default changed; re-read this test"
    for f in (0.0, 0.5, 1.0):
        assert rg.grip_q(side, f)[i] == pytest.approx(0.0)


@pytest.mark.parametrize("side", SIDES)
def test_the_grip_is_monotonic_from_open_to_closed(side):
    """No joint may turn around on the way: a grasp tightens all the way in."""
    for i, n in enumerate(_names(side)):
        seq = [rg.grip_q(side, f / 10.0)[i] for f in range(11)]
        diffs = [b - a for a, b in zip(seq, seq[1:])]
        assert all(d >= -1e-9 for d in diffs) or all(d <= 1e-9 for d in diffs), n
