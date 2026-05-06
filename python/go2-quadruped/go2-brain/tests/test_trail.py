"""Unit tests for brain.trail.

Pure-Python — no DDS. Each test scripts a sequence of (FusedTarget, Pose)
appends and verifies the body-frame goal returned by goal_for_recovery.
"""

from __future__ import annotations

import math
import unittest
from unittest import mock

from brain.fusion import FusedTarget
from brain.models import Pose
from brain.trail import GhostTrail, TRAIL_APPROACH_S, TRAIL_EXTRAPOLATE_S


def _target(bearing_deg: float = 0.0, distance_m: float = 1.0) -> FusedTarget:
    return FusedTarget(
        bearing_deg=bearing_deg,
        distance_m=distance_m,
        bearing_source="uwb",
        confidence=0.9,
        target_vx_body=0.0,
        target_vy_body=0.0,
        age_s=0.0,
        tracking_state="TRACKING",
        vision_track_id=None,
        is_followable=True,
    )


def _pose(x: float = 0.0, y: float = 0.0, yaw: float = 0.0) -> Pose:
    return Pose(x_m=x, y_m=y, yaw_rad=yaw)


class TestAppendAndPrune(unittest.TestCase):
    def test_no_pose_drops_sample(self):
        trail = GhostTrail()
        trail.append(_target(), pose=None)
        self.assertEqual(len(trail), 0)

    def test_appends_grow(self):
        trail = GhostTrail()
        trail.append(_target(), _pose())
        trail.append(_target(), _pose())
        self.assertEqual(len(trail), 2)

    def test_prune_history(self):
        trail = GhostTrail()
        with mock.patch("brain.trail.time.monotonic") as fake:
            fake.return_value = 100.0
            trail.append(_target(), _pose())
            fake.return_value = 110.0  # well past TRAIL_HISTORY_S
            trail.append(_target(), _pose())
            self.assertEqual(len(trail), 1)


class TestGoalRecovery(unittest.TestCase):
    def test_empty_trail_returns_none(self):
        trail = GhostTrail()
        self.assertIsNone(trail.goal_for_recovery(_pose(), 0.0))

    def test_no_pose_on_read_returns_none(self):
        trail = GhostTrail()
        trail.append(_target(distance_m=2.0), _pose())
        self.assertIsNone(trail.goal_for_recovery(None, 0.0))

    def test_phase_a_returns_last_seen_in_body(self):
        trail = GhostTrail()
        # Operator was 2 m ahead of dog at world (0, 0, yaw=0).
        trail.append(_target(bearing_deg=0.0, distance_m=2.0), _pose())
        # Recovery starts immediately — dog hasn't moved.
        goal = trail.goal_for_recovery(_pose(), 0.0)
        assert goal is not None
        self.assertFalse(goal.extrapolated)
        self.assertAlmostEqual(goal.body_x, 2.0, places=5)
        self.assertAlmostEqual(goal.body_y, 0.0, places=5)

    def test_phase_a_dog_moves_goal_updates(self):
        trail = GhostTrail()
        # Operator at world (3, 0); dog at (0, 0, 0).
        trail.append(_target(bearing_deg=0.0, distance_m=3.0), _pose())
        # Dog drove forward 1 m. Goal in body frame is now 2 m ahead.
        goal = trail.goal_for_recovery(_pose(x=1.0), 0.0)
        assert goal is not None
        self.assertAlmostEqual(goal.body_x, 2.0, places=5)
        self.assertAlmostEqual(goal.body_y, 0.0, places=5)

    def test_phase_b_extrapolates_along_tangent(self):
        # Operator walked from (1,0) to (2,1) over 0.5 s, then vanished.
        trail = GhostTrail()
        with mock.patch("brain.trail.time.monotonic") as fake:
            fake.return_value = 100.0
            trail.append(
                _target(bearing_deg=0.0, distance_m=1.0), _pose()
            )
            fake.return_value = 100.5
            # Operator at world (2, 1); dog still at (0, 0, 0).
            # In body frame: distance=sqrt(5), bearing=atan2(1,2).
            d = math.hypot(2, 1)
            b = math.degrees(math.atan2(1, 2))
            trail.append(_target(bearing_deg=b, distance_m=d), _pose())
            # Now enter Phase B at TRAIL_APPROACH_S + 0.5.
            fake.return_value = 100.5 + TRAIL_APPROACH_S + 0.5
            goal = trail.goal_for_recovery(
                _pose(), TRAIL_APPROACH_S + 0.5
            )
        assert goal is not None
        self.assertTrue(goal.extrapolated)
        # Tangent direction = (2-1, 1-0)/0.5 = (2, 2) m/s in world frame.
        # After 0.5 s of extrapolation, virtual point at (2+2*0.5, 1+2*0.5)
        # = (3, 2). Dog still at (0,0,0) so body == world.
        self.assertAlmostEqual(goal.body_x, 3.0, places=4)
        self.assertAlmostEqual(goal.body_y, 2.0, places=4)

    def test_phase_b_falls_back_to_last_seen_with_one_entry(self):
        trail = GhostTrail()
        trail.append(_target(distance_m=2.0), _pose())
        goal = trail.goal_for_recovery(
            _pose(), TRAIL_APPROACH_S + 0.5
        )
        assert goal is not None
        self.assertTrue(goal.extrapolated)
        self.assertAlmostEqual(goal.body_x, 2.0, places=5)

    def test_phase_b_exhausted_returns_none(self):
        trail = GhostTrail()
        trail.append(_target(distance_m=2.0), _pose())
        goal = trail.goal_for_recovery(
            _pose(), TRAIL_APPROACH_S + TRAIL_EXTRAPOLATE_S + 0.1
        )
        self.assertIsNone(goal)

    def test_corner_recovery_geometry(self):
        """Operator turned a corner; ghost trail should drive the dog
        toward the corner, not straight forward."""
        trail = GhostTrail()
        with mock.patch("brain.trail.time.monotonic") as fake:
            # Operator path in world coords: (3,0) → (2,1) → (1,1.5) → vanish.
            for i, (px, py) in enumerate([(3, 0), (2, 1), (1, 1.5)]):
                fake.return_value = 100.0 + 0.3 * i
                # Dog stays at origin facing +x.
                d = math.hypot(px, py)
                b = math.degrees(math.atan2(py, px))
                trail.append(
                    _target(bearing_deg=b, distance_m=d), _pose()
                )
            # Recovery — Phase A drives to last seen (1, 1.5) which is
            # forward-left-ish, NOT straight forward. Body-y > 0 confirms
            # the dog is being redirected leftward.
            fake.return_value = 100.6 + 0.0
            goal = trail.goal_for_recovery(_pose(), 0.0)
        assert goal is not None
        self.assertGreater(goal.body_y, 0.5)
        self.assertAlmostEqual(goal.body_x, 1.0, places=4)


class TestWorldFrameStability(unittest.TestCase):
    """Dog rotation/translation must not contaminate trail entries."""

    def test_dog_yaw_flips_body_frame_correctly(self):
        trail = GhostTrail()
        # Operator world position: (2, 0). Dog at (0,0) facing +x → body
        # frame matches world.
        trail.append(_target(bearing_deg=0.0, distance_m=2.0), _pose())
        # Now dog has rotated 90° CCW (yaw=+pi/2). World x → body y.
        goal = trail.goal_for_recovery(
            _pose(yaw=math.pi / 2), 0.0
        )
        assert goal is not None
        # World point (2,0) in body frame of dog at (0,0,yaw=pi/2):
        # body_x = cos(-pi/2)*dx - sin(-pi/2)*dy = 0*2 - (-1)*0 = 0
        # body_y = sin(-pi/2)*dx + cos(-pi/2)*dy = (-1)*2 + 0*0 = -2
        self.assertAlmostEqual(goal.body_x, 0.0, places=5)
        self.assertAlmostEqual(goal.body_y, -2.0, places=5)


if __name__ == "__main__":
    unittest.main()
