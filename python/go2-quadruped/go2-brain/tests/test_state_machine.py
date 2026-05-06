"""Smoke tests for the FSM after the FusedTarget + TrailGoal rewire.

Exercises the state transitions and the recovery branches (with and
without a trail goal). Doesn't replay every previous behaviour — those
are covered implicitly by the proportional control law staying intact.
"""

from __future__ import annotations

import unittest
from unittest import mock

from brain.fusion import FusedTarget, TargetFuser
from brain.models import Decision
from brain.state_machine import Action, ParamStore, Params, State, StateMachine
from brain.trail import TrailGoal


def _ftarget(
    bearing_deg: float = 0.0,
    distance_m: float = 1.0,
    confidence: float = 0.95,
    state: str = "TRACKING",
    is_followable: bool = True,
) -> FusedTarget:
    return FusedTarget(
        bearing_deg=bearing_deg,
        distance_m=distance_m,
        bearing_source="uwb",
        confidence=confidence,
        target_vx_body=0.0,
        target_vy_body=0.0,
        age_s=0.0,
        tracking_state=state,
        vision_track_id=None,
        is_followable=is_followable,
    )


def _store() -> ParamStore:
    return ParamStore(Params(), path=None)


class TestFollowing(unittest.TestCase):
    def test_idle_to_following(self):
        fsm = StateMachine(_store())
        tick = fsm.step(_ftarget())
        self.assertEqual(tick.state, State.FOLLOWING)

    def test_acquiring_searches(self):
        """Sanity check using the helper: ACQUIRING goes to SEARCHING. The
        end-to-end version (real fuser) lives in TestColdStart below."""
        fsm = StateMachine(_store())
        tick = fsm.step(_ftarget(state="ACQUIRING", is_followable=False))
        self.assertEqual(tick.state, State.SEARCHING)
        self.assertEqual(tick.action, Action.HOLD)


class TestColdStart(unittest.TestCase):
    """Realistic cold-start path: feed Decisions through the actual fuser
    into the FSM. Catches the bug where the fuser used to return None for
    ACQUIRING, leaving the FSM stuck in IDLE during the settle window."""

    def test_acquiring_drives_idle_to_searching(self):
        fuser = TargetFuser()
        fsm = StateMachine(_store())
        decision = Decision(
            tracking_state="ACQUIRING",
            bearing_deg=2.0,
            distance_m=2.5,
            confidence=0.6,
        )
        target = fuser.fuse(decision, 0.05, None, None)
        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target.tracking_state, "ACQUIRING")
        self.assertFalse(target.is_followable)
        tick = fsm.step(target)
        self.assertEqual(tick.state, State.SEARCHING)
        self.assertEqual(tick.action, Action.HOLD)

    def test_acquiring_then_tracking_reaches_following(self):
        """Full cold-start: ACQUIRING → TRACKING transition reaches
        FOLLOWING through SEARCHING, the way the previous brain did."""
        fuser = TargetFuser()
        fsm = StateMachine(_store())
        # ACQUIRING phase.
        target = fuser.fuse(
            Decision(tracking_state="ACQUIRING", bearing_deg=0.0,
                     distance_m=1.5, confidence=0.6),
            0.05, None, None,
        )
        tick = fsm.step(target)
        self.assertEqual(tick.state, State.SEARCHING)
        # UWB promotes to TRACKING.
        target = fuser.fuse(
            Decision(tracking_state="TRACKING", bearing_deg=0.0,
                     distance_m=1.5, confidence=0.95),
            0.05, None, None,
        )
        tick = fsm.step(target)
        self.assertEqual(tick.state, State.FOLLOWING)

    def test_low_confidence_searches_with_hysteresis(self):
        fsm = StateMachine(_store())
        # Enter FOLLOWING.
        fsm.step(_ftarget(confidence=0.95))
        self.assertEqual(fsm.state, State.FOLLOWING)
        # Drop just below exit threshold (0.3 default).
        tick = fsm.step(_ftarget(confidence=0.25))
        self.assertEqual(tick.state, State.SEARCHING)


class TestRecovery(unittest.TestCase):
    def test_ride_through_then_recover_with_trail(self):
        p = Params()
        fsm = StateMachine(ParamStore(p, path=None))
        # Build up FOLLOWING.
        for _ in range(3):
            fsm.step(_ftarget())
        self.assertEqual(fsm.state, State.FOLLOWING)
        # Burn the ride-through window with un-followable targets.
        for _ in range(p.lost_ride_through_ticks):
            tick = fsm.step(_ftarget(is_followable=False))
            self.assertEqual(tick.state, State.FOLLOWING)  # ride-through
        # Next un-followable tick triggers RECOVERING; we provide a trail.
        goal = TrailGoal(body_x=2.0, body_y=1.0, age_s=0.1, extrapolated=False)
        tick = fsm.step(_ftarget(is_followable=False), trail_goal=goal)
        self.assertEqual(tick.state, State.RECOVERING)
        # Trail goal at (2, 1) is forward and to the left → expect +vyaw
        # and +vx (the dog isn't yet aligned but is within the cone).
        self.assertGreater(tick.vyaw, 0)
        self.assertIn("trail approach", tick.reason)

    def test_recovery_without_trail_falls_back_to_spin(self):
        p = Params()
        fsm = StateMachine(ParamStore(p, path=None))
        # FOLLOWING with bearing to the left so last_seen_bearing is +ve.
        fsm.step(_ftarget(bearing_deg=15.0))
        # Ride through and tip into RECOVERING.
        for _ in range(p.lost_ride_through_ticks + 1):
            fsm.step(_ftarget(is_followable=False))
        tick = fsm.step(_ftarget(is_followable=False), trail_goal=None)
        self.assertEqual(tick.state, State.RECOVERING)
        self.assertEqual(tick.vx, 0.0)
        self.assertGreater(tick.vyaw, 0)  # spin left toward last bearing
        self.assertIn("no trail", tick.reason)

    def test_recovery_returns_to_following_on_good_target(self):
        p = Params()
        fsm = StateMachine(ParamStore(p, path=None))
        fsm.step(_ftarget())
        for _ in range(p.lost_ride_through_ticks + 1):
            fsm.step(_ftarget(is_followable=False))
        self.assertEqual(fsm.state, State.RECOVERING)
        # Good sample arrives.
        tick = fsm.step(_ftarget())
        self.assertEqual(tick.state, State.FOLLOWING)

    def test_recovery_timeout_idles(self):
        p = Params()
        fsm = StateMachine(ParamStore(p, path=None))
        fsm.step(_ftarget())
        # Force into RECOVERING.
        for _ in range(p.lost_ride_through_ticks + 1):
            fsm.step(_ftarget(is_followable=False))
        self.assertEqual(fsm.state, State.RECOVERING)
        with mock.patch("brain.state_machine.time.monotonic") as fake:
            fake.return_value = (fsm._recovering_since or 0) + p.recovery_timeout_s + 0.5
            tick = fsm.step(_ftarget(is_followable=False))
        self.assertEqual(tick.state, State.IDLE)


class TestRecoverElapsedProperty(unittest.TestCase):
    def test_elapsed_zero_when_not_recovering(self):
        fsm = StateMachine(_store())
        self.assertEqual(fsm.recover_elapsed_s, 0.0)

    def test_elapsed_grows_in_recovering(self):
        p = Params()
        fsm = StateMachine(ParamStore(p, path=None))
        fsm.step(_ftarget())
        for _ in range(p.lost_ride_through_ticks + 1):
            fsm.step(_ftarget(is_followable=False))
        self.assertEqual(fsm.state, State.RECOVERING)
        self.assertGreaterEqual(fsm.recover_elapsed_s, 0.0)
        with mock.patch("brain.state_machine.time.monotonic") as fake:
            fake.return_value = (fsm._recovering_since or 0) + 1.5
            self.assertAlmostEqual(fsm.recover_elapsed_s, 1.5, places=3)


if __name__ == "__main__":
    unittest.main()
