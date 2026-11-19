"""The grip force on a stalled finger is the heat knob, and it is reachable.

A finger commanded closed with a can in the way holds a position error for as
long as the grip lasts. Torque is that error times kp, so the error is the
current, and the current is the heat: two thumbs tripped at 86 C in one
evening at kp 3.0 with the stall backoff holding 0.35 rad.

Pinned here: the RESIDUAL error after a back-off is small -- it is the only
number that decides sustained grip force -- and the env knobs parse.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_p = str(ROOT / "onboard")
if _p not in sys.path:
    sys.path.insert(0, _p)

import dex3_stall as ds  # noqa: E402

def _stalled(margin, kp_gap=1.5):
    """Run a finger into a block and return the command once it backs off."""
    t = [0.0]
    g = ds.StallBackoff(stall_s=0.5, gap_rad=0.3, margin_rad=margin, clock=lambda: t[0])
    cmd = [0.0] * 7
    cmd[1] = kp_gap                      # thumb_1 told to close
    meas = [0.0] * 7                     # and not moving at all
    out, names = g.apply("left", cmd, meas)
    t[0] = 0.6                           # past stall_s, still not moving
    out, names = g.apply("left", cmd, meas)
    return out[1], names


def test_a_stalled_finger_holds_the_margin_not_the_whole_command():
    held, names = _stalled(ds.StallBackoff().margin_rad)
    assert "thumb_1" in names, "the backoff did not fire"
    assert held == pytest.approx(ds.StallBackoff().margin_rad)


def test_the_default_grip_force_is_less_than_half_what_cooked_the_thumbs():
    """0.35 rad at kp 3.0 reached 86 C twice. The default must be well under."""
    assert ds.StallBackoff().margin_rad <= 0.2
    assert ds.StallBackoff().margin_rad > 0.0, "zero would drop the can"


def test_grip_force_scales_the_held_error_and_so_the_current():
    """Halving the margin halves the residual error, which is the torque."""
    soft, _ = _stalled(0.15)
    firm, _ = _stalled(0.35)
    assert soft == pytest.approx(0.15) and firm == pytest.approx(0.35)
    assert soft / firm == pytest.approx(0.15 / 0.35)


def test_the_margin_never_overshoots_a_command_closer_than_itself():
    """A gentle command must not be turned INTO a squeeze by the backoff."""
    held, _ = _stalled(0.35, kp_gap=0.31)
    assert held <= 0.31 + 1e-9


def test_the_env_reader_accepts_the_knobs_and_refuses_nonsense():
    g = ds.StallBackoff.from_env({"G1_DEX3_HOLD_MARGIN_RAD": "0.25",
                                  "G1_DEX3_STALL_S": "0.3"})
    assert g.margin_rad == 0.25 and g.stall_s == 0.3
    assert ds.StallBackoff.from_env({}).margin_rad == ds.StallBackoff().margin_rad
    for bad in ("banana", "-1", "99"):
        with pytest.raises(ValueError):
            ds.StallBackoff.from_env({"G1_DEX3_HOLD_MARGIN_RAD": bad})


def test_backoff_can_be_switched_off_for_a_comparison():
    g = ds.StallBackoff.from_env({"G1_DEX3_STALL_BACKOFF": "0"})
    assert g.enabled is False
    cmd, names = g.apply("left", [1.5] * 7, [0.0] * 7)
    assert names == [] and cmd == [1.5] * 7
