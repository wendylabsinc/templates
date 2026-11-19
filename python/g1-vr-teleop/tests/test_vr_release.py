"""Handing the robot back, from inside the headset.

The operator could arm, home, record and mark an outcome on the controllers
but not give the robot back. Doing it meant taking the headset off -- which
ends the XR session and drops the socket, the same trap arming was in before
arm_on_sticks.

`relax` is the release that matters: it shuts down the arm_sdk publisher.
While that publisher runs it asserts control at full weight and the robot's
own controller fights it, so "the Unitree remote does nothing" is what a
still-running bridge looks like from the operator's side.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_p = str(Path(__file__).resolve().parent.parent / "teleop" / "bridge")
if _p not in sys.path:
    sys.path.insert(0, _p)

import server as srv  # noqa: E402

HOLD = srv.RELEASE_HOLD_S


class Sink:
    onboard_state = "HOLDING — arms frozen"

    def __init__(self):
        self.cmds = []

    def command(self, name, payload=None):
        self.cmds.append(name)
        return True


def _frame(right=False, left=False):
    return {"hands": [
        {"hand": "right", "source": "controller", "buttons": {"stick": right}},
        {"hand": "left", "source": "controller", "buttons": {"stick": left}},
    ]}


def _app(sink):
    return {"sink": sink}


def test_a_held_right_stick_releases_after_the_hold():
    sink, stats = Sink(), {}
    srv.release_on_stick_hold(_frame(right=True), stats, _app(sink), now=0.0)
    assert sink.cmds == [], "fired on the press, not the hold"
    srv.release_on_stick_hold(_frame(right=True), stats, _app(sink), now=HOLD - 0.01)
    assert sink.cmds == [], "fired early"
    assert srv.release_on_stick_hold(_frame(right=True), stats, _app(sink), now=HOLD) is True
    assert sink.cmds == ["relax"]


def test_it_fires_once_per_hold_not_every_frame():
    sink, stats = Sink(), {}
    for t in (0.0, HOLD, HOLD + 0.02, HOLD + 5.0):
        srv.release_on_stick_hold(_frame(right=True), stats, _app(sink), now=t)
    assert sink.cmds == ["relax"], f"released {len(sink.cmds)} times"


def test_a_click_does_nothing():
    """Every other control fires on an edge; this one must not."""
    sink, stats = Sink(), {}
    srv.release_on_stick_hold(_frame(right=True), stats, _app(sink), now=0.0)
    srv.release_on_stick_hold(_frame(), stats, _app(sink), now=0.1)
    srv.release_on_stick_hold(_frame(right=True), stats, _app(sink), now=0.2)
    srv.release_on_stick_hold(_frame(), stats, _app(sink), now=0.3)
    assert sink.cmds == []


def test_the_arming_press_never_releases_however_staggered():
    """Both sticks is arming. A right-first press with a late left must not
    release on the way -- the hazard this shares with the miniature toggle."""
    for lag in (0.05, 0.5, HOLD - 0.01, HOLD + 2.0):
        sink, stats = Sink(), {}
        srv.release_on_stick_hold(_frame(right=True), stats, _app(sink), now=0.0)
        # the left arrives, late; from here it is an arming press
        for t in (lag, lag + HOLD, lag + 2 * HOLD):
            srv.release_on_stick_hold(_frame(right=True, left=True), stats, _app(sink), now=t)
        assert sink.cmds == [], f"released during a {lag}s-staggered arming press"


def test_the_cancel_lasts_until_the_stick_comes_up():
    """Releasing the left mid-hold must not resume the countdown."""
    sink, stats = Sink(), {}
    srv.release_on_stick_hold(_frame(right=True), stats, _app(sink), now=0.0)
    srv.release_on_stick_hold(_frame(right=True, left=True), stats, _app(sink), now=0.1)
    srv.release_on_stick_hold(_frame(right=True), stats, _app(sink), now=0.2)
    srv.release_on_stick_hold(_frame(right=True), stats, _app(sink), now=10.0)
    assert sink.cmds == []
    # Both up, then a clean hold: allowed again.
    srv.release_on_stick_hold(_frame(), stats, _app(sink), now=10.1)
    srv.release_on_stick_hold(_frame(right=True), stats, _app(sink), now=10.2)
    srv.release_on_stick_hold(_frame(right=True), stats, _app(sink), now=10.2 + HOLD)
    assert sink.cmds == ["relax"]


def test_a_tracked_hand_has_no_stick_and_never_releases():
    sink, stats = Sink(), {}
    frame = {"hands": [{"hand": "right", "source": "hand", "landmarks": [[0, 0, 0]]}]}
    for t in (0.0, HOLD, 2 * HOLD):
        srv.release_on_stick_hold(frame, stats, _app(sink), now=t)
    assert sink.cmds == []


def test_a_sink_that_cannot_command_is_not_a_crash():
    stats = {}
    assert srv.release_on_stick_hold(_frame(right=True), stats, {"sink": object()}, now=0.0) is False
    assert srv.release_on_stick_hold(_frame(right=True), stats, {}, now=HOLD) is False


def test_the_hold_is_long_enough_to_be_a_decision():
    assert HOLD >= 1.0, "a slip while reaching for the arming press would release"


def test_the_legend_tells_the_operator_it_exists():
    js = (Path(__file__).resolve().parent.parent / "teleop" / "web" / "hud.js").read_text()
    assert "right stick (hold)" in js and "Give robot back" in js
