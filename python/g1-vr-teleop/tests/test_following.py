"""Catching "powered on, but not in Main Operation Control".

This condition looks healthy in every other field: lowstate publishes, so
lowstate_ok() is true; take_control succeeds; arm succeeds; frames are applied.
The arms simply do not move, because rt/arm_sdk blends into a motion controller
that is not running. The Dex3 hands are on their own topic and keep working
throughout, so the operator sees fingers respond and concludes the arms are
broken.

It cost a long detour today, and the operator diagnosed it, not the tooling.
"""
import math
import os
import sys
import threading

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "onboard"))
import g1_onboard as ob  # noqa: E402


def _cmd():
    c = ob.Commander.__new__(ob.Commander)
    c.lock = threading.RLock()
    c._track_err = []
    c.n_leashed = 0
    c._leash_log_t = 0.0
    c._last_cmd = None
    return c


def _feed(c, n, commanded_deg, moved_deg):
    """n frames where the command sweeps `commanded_deg` and the arm `moved_deg`."""
    for i in range(n):
        f = i / max(1, n - 1)
        c.measured_arm_q = [math.radians(moved_deg) * f] + [0.0] * 13
        q = np.array([math.radians(commanded_deg) * f] + [0.0] * 13)
        c._leash(q, float(i))


def test_a_commanded_but_motionless_arm_is_named():
    c = _cmd()
    _feed(c, 60, commanded_deg=80, moved_deg=0.2)
    r = c.following_report()
    assert "verdict" in r, r
    assert "NOT MOVING" in r["verdict"]
    assert "Main Operation Control" in r["verdict"], \
        "the verdict should name the likely cause, not just the symptom"
    assert r["commanded_deg"] > 50 and r["moved_deg"] < 5


def test_an_arm_that_follows_is_not_flagged():
    c = _cmd()
    _feed(c, 60, commanded_deg=80, moved_deg=78)
    r = c.following_report()
    assert "verdict" not in r, r


def test_a_partly_following_arm_is_not_flagged():
    """Lagging is a different condition and has different fixes."""
    c = _cmd()
    _feed(c, 60, commanded_deg=80, moved_deg=40)
    assert "verdict" not in c.following_report()


def test_a_still_operator_does_not_trip_it():
    """No command means no evidence -- it must not read as a fault."""
    c = _cmd()
    _feed(c, 60, commanded_deg=0.5, moved_deg=0.0)
    r = c.following_report()
    assert "verdict" not in r, "a motionless operator was reported as a fault"


def test_it_says_nothing_until_it_has_evidence():
    c = _cmd()
    _feed(c, 5, commanded_deg=80, moved_deg=0.0)
    r = c.following_report()
    assert "verdict" not in r and r["samples"] == 5


def test_it_survives_a_commander_built_without_init():
    """Commander is built by __new__ in several paths; a diagnostic must not
    be the thing that breaks them."""
    c = _cmd()
    assert c.following_report()["samples"] == 0
    _feed(c, 40, commanded_deg=60, moved_deg=0.1)
    assert c.following_report()["samples"] == 40


def test_cancel_keeps_the_saved_calibration():
    """There was no way to say "never mind" to a calibration.

    calibrate_stop APPLIES what was walked, so a recording started and then
    abandoned would, on stop, reset every unexercised joint to its mechanical
    limit -- losing a good calibration. calibrate_clear deletes the saved one
    outright. Neither is "never mind", and the situation arises the moment
    calibrate_start reports it is starting from scratch when you only meant to
    fix one joint.
    """
    c = _cmd()
    c.cal = {"lo": [0.0] * 14, "hi": [0.0] * 14}
    msg = c.calibrate_cancel()
    assert "untouched" in msg
    assert c.cal is None
    # and it is safe to call twice
    assert c.calibrate_cancel() == "not calibrating"


def _claimable():
    c = _cmd()
    c.owner = None
    c.last_rx_t = 0.0
    c.state = "STANDBY"
    c._frozen_at = None
    return c


def test_a_live_operator_cannot_be_displaced():
    """The whole point of the claim: a stray tab must not steal the arms."""
    import time as _t
    c = _claimable()
    tok = c.claim()
    assert tok is not None
    c.last_rx_t = _t.time()          # frames arriving now
    assert c.claim() is None, "a live operator was taken over"


def test_a_silent_owner_can_be_displaced():
    """A half-open socket never runs its disconnect handler.

    Observed: redeploying the bridge left the claim held, and every new bridge
    was refused with "another operator holds control" with no way back short of
    restarting the robot's app.
    """
    import time as _t
    c = _claimable()
    first = c.claim()
    c.last_rx_t = _t.time() - (c.OWNER_STALE_S + 1)
    second = c.claim()
    assert second is not None, "a dead socket kept the robot locked out"
    assert second != first


def test_a_takeover_freezes_rather_than_continues():
    """A change of operator is not a moment to keep moving."""
    import time as _t
    c = _claimable()
    c.claim()
    c.state = "ARMED"
    c.last_rx_t = _t.time() - (c.OWNER_STALE_S + 1)
    c.claim()
    assert c.state != "ARMED", "the arms kept following through an operator change"


def test_the_stale_window_is_well_clear_of_a_tracking_dropout():
    """Otherwise a dropout would hand control away mid-session."""
    c = _claimable()
    assert c.OWNER_STALE_S > 5 * 1.0      # the liveness watchdog is 1 s
