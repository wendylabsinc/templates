"""Unit tests for brain.fusion.

Pure-Python — no DDS, no real time. Each test scripts a sequence of
(Decision, VisionTracks) inputs to TargetFuser.fuse and asserts
properties of the FusedTarget output.

Run:
    PYTHONPATH=src python3 -m unittest tests.test_fusion -v
"""

from __future__ import annotations

import math
import unittest
from typing import Optional
from unittest import mock

from brain.fusion import FusedTarget, TargetFuser
from brain.models import Decision, VisionTrack, VisionTracks


def _decision(
    state: str = "TRACKING",
    bearing_deg: Optional[float] = 0.0,
    distance_m: Optional[float] = 1.5,
    confidence: float = 0.95,
    closing: Optional[float] = None,
    lateral: Optional[float] = None,
) -> Decision:
    return Decision(
        tracking_state=state,
        bearing_deg=bearing_deg,
        distance_m=distance_m,
        confidence=confidence,
        closing_rate_mps=closing,
        lateral_rate_mps=lateral,
    )


def _track(
    tid: str = "1",
    bearing_deg: float = 0.0,
    distance_m: float = 1.5,
    age_frames: int = 20,
    dist_confidence: float = 0.95,
    score: float = 0.9,
) -> VisionTrack:
    return VisionTrack(
        id=tid,
        bearing_deg=bearing_deg,
        distance_m=distance_m,
        dist_confidence=dist_confidence,
        age_frames=age_frames,
        score=score,
    )


def _tracks(*ts: VisionTrack) -> VisionTracks:
    return VisionTracks(tracks=list(ts))


class TestUWBOnly(unittest.TestCase):
    """When vision is missing or stale, fusion should mirror UWB."""

    def test_uwb_only(self):
        f = TargetFuser()
        out = f.fuse(_decision(), 0.05, None, None)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out.bearing_source, "uwb")
        self.assertEqual(out.tracking_state, "TRACKING")
        self.assertAlmostEqual(out.bearing_deg, 0.0)
        self.assertAlmostEqual(out.distance_m, 1.5)
        self.assertTrue(out.is_followable)
        self.assertIsNone(out.vision_track_id)

    def test_uwb_lost_no_vision_returns_none(self):
        f = TargetFuser()
        out = f.fuse(_decision(state="LOST"), 0.05, None, None)
        self.assertIsNone(out)

    def test_uwb_acquiring_produces_non_followable_target(self):
        """ACQUIRING is a real signal during cold start, not 'no signal'.
        The fuser must produce a non-None FusedTarget so the FSM can route
        IDLE → SEARCHING during the UWB filter's ~1 s settle window."""
        f = TargetFuser()
        out = f.fuse(_decision(state="ACQUIRING", confidence=0.6), 0.05, None, None)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out.tracking_state, "ACQUIRING")
        self.assertEqual(out.bearing_source, "uwb")
        # Not yet followable — drives SEARCHING, not FOLLOWING.
        self.assertFalse(out.is_followable)

    def test_uwb_closing_lateral_passthrough(self):
        f = TargetFuser()
        out = f.fuse(
            _decision(closing=0.3, lateral=-0.2), 0.05, None, None
        )
        assert out is not None
        self.assertAlmostEqual(out.target_vx_body, 0.3)
        self.assertAlmostEqual(out.target_vy_body, -0.2)


class TestFusion(unittest.TestCase):
    """UWB + vision both present and agreeing."""

    def test_agreement_fuses_weighted(self):
        f = TargetFuser()
        d = _decision(bearing_deg=10.0, distance_m=2.0, confidence=0.9)
        t = _track(bearing_deg=12.0, distance_m=2.1, dist_confidence=0.9, age_frames=30)
        out = f.fuse(d, 0.05, _tracks(t), 0.05)
        assert out is not None
        self.assertEqual(out.bearing_source, "fused")
        # Weighted average lies between the two inputs.
        self.assertGreater(out.bearing_deg, 10.0)
        self.assertLess(out.bearing_deg, 12.0)
        self.assertGreater(out.distance_m, 2.0)
        self.assertLess(out.distance_m, 2.1)
        self.assertEqual(out.vision_track_id, "1")

    def test_disagreement_drops_lower_weight(self):
        f = TargetFuser()
        d = _decision(bearing_deg=20.0, confidence=0.95)  # high UWB confidence
        # Vision claims the operator is at -20° — 40° apart, beyond gate.
        t = _track(bearing_deg=-20.0, dist_confidence=0.5, age_frames=15)
        out = f.fuse(d, 0.05, _tracks(t), 0.05)
        assert out is not None
        # UWB is heavier-weighted → vision dropped, source = uwb.
        self.assertEqual(out.bearing_source, "uwb")
        self.assertAlmostEqual(out.bearing_deg, 20.0)

    def test_disagreement_drops_uwb_when_vision_heavier(self):
        """Realistic scenario: vision had locked the operator while UWB
        was healthy; UWB then drifts to a wrong bearing. The cached
        track ID lets vision win the disagreement gate."""
        f = TargetFuser()
        # Tick 1: both healthy and agreeing — cache the active track ID.
        f.fuse(
            _decision(bearing_deg=0.0, confidence=0.9),
            0.05,
            _tracks(_track(tid="op", bearing_deg=0.0, age_frames=30)),
            0.05,
        )
        # Tick 2: UWB drifts to +40° (multipath ghost), vision still has
        # the same track at the original bearing.
        d = _decision(bearing_deg=40.0, confidence=0.4, state="PREDICTING")
        t = _track(tid="op", bearing_deg=0.0, dist_confidence=1.0, age_frames=50)
        out = f.fuse(d, 0.05, _tracks(t), 0.05)
        assert out is not None
        self.assertEqual(out.bearing_source, "vision")
        self.assertAlmostEqual(out.bearing_deg, 0.0)


class TestActiveTrackId(unittest.TestCase):
    """Cached track ID survives short vision dropouts; reset eventually."""

    def test_id_caches_across_ticks(self):
        f = TargetFuser()
        d = _decision(bearing_deg=0.0)
        t = _track(tid="42", bearing_deg=2.0)
        out = f.fuse(d, 0.05, _tracks(t), 0.05)
        assert out is not None
        self.assertEqual(out.vision_track_id, "42")
        # Next tick: same track still present.
        out = f.fuse(d, 0.05, _tracks(t), 0.05)
        assert out is not None
        self.assertEqual(out.vision_track_id, "42")

    def test_id_persists_when_uwb_drops(self):
        f = TargetFuser()
        d_ok = _decision(bearing_deg=0.0)
        t = _track(tid="42", bearing_deg=2.0)
        # First lock with both.
        f.fuse(d_ok, 0.05, _tracks(t), 0.05)
        # UWB goes LOST; vision still sees track 42.
        out = f.fuse(_decision(state="LOST"), 0.05, _tracks(t), 0.05)
        assert out is not None
        self.assertEqual(out.bearing_source, "vision")
        self.assertEqual(out.vision_track_id, "42")
        self.assertEqual(out.tracking_state, "TRACKING")

    def test_crowd_picks_closest_bearing(self):
        f = TargetFuser()
        d = _decision(bearing_deg=15.0)
        # Three tracks, one matches UWB bearing within MATCH_BEARING_DEG=20.
        ts = _tracks(
            _track(tid="1", bearing_deg=-30.0, age_frames=30),
            _track(tid="2", bearing_deg=14.0, age_frames=30),
            _track(tid="3", bearing_deg=40.0, age_frames=30),
        )
        out = f.fuse(d, 0.05, ts, 0.05)
        assert out is not None
        self.assertEqual(out.vision_track_id, "2")

    def test_id_resets_after_long_loss(self):
        f = TargetFuser()
        d = _decision(bearing_deg=0.0)
        t = _track(tid="42", bearing_deg=2.0)
        f.fuse(d, 0.05, _tracks(t), 0.05)
        # Simulate 4 s of "track 42 missing entirely" by patching monotonic.
        with mock.patch("brain.fusion.time.monotonic") as fake:
            base = 100.0
            fake.return_value = base
            f._active_id_seen_at = base  # type: ignore[attr-defined]
            fake.return_value = base + 5.0
            f.fuse(_decision(bearing_deg=0.0), 0.05, _tracks(), 0.05)
            self.assertIsNone(f.active_track_id)

    def test_young_track_ignored(self):
        """Tracks below MIN_TRACK_AGE_FRAMES aren't trusted for association
        during *initial* acquisition — no prior lock, so we stay strict."""
        f = TargetFuser()
        d = _decision(bearing_deg=0.0)
        t = _track(tid="brandnew", age_frames=1)
        out = f.fuse(d, 0.05, _tracks(t), 0.05)
        assert out is not None
        self.assertEqual(out.bearing_source, "uwb")
        self.assertIsNone(out.vision_track_id)

    def test_reassoc_picks_up_fresh_id_immediately(self):
        """Once we've had a lock, a BYTETrack ID flip (operator briefly out
        and back, fresh id with age=0 at the same UWB bearing) is picked up
        on the very next tick — not after MIN_TRACK_AGE_FRAMES."""
        f = TargetFuser()
        # Establish initial lock with a properly-aged track.
        f.fuse(
            _decision(bearing_deg=0.0, confidence=0.9),
            0.05,
            _tracks(_track(tid="42", bearing_deg=0.0, age_frames=30)),
            0.05,
        )
        self.assertEqual(f.active_track_id, "42")
        # Operator briefly out; BYTETrack drops "42" and emits a fresh "43"
        # at age 0 at the same UWB bearing. UWB still healthy.
        out = f.fuse(
            _decision(bearing_deg=0.0, confidence=0.9),
            0.05,
            _tracks(_track(tid="43", bearing_deg=1.0, age_frames=0)),
            0.05,
        )
        # The fuser identifies the new track as the operator immediately.
        # (Vision *weight* still ramps up via age_factor, so the bearing
        # source on this tick is UWB, but the ID handoff has happened.)
        self.assertEqual(f.active_track_id, "43")
        assert out is not None
        self.assertEqual(out.vision_track_id, "43")

    def test_no_reassoc_without_prior_lock(self):
        """Without a previous lock, the loosening must not apply — fresh
        tracks remain too-young until they reach MIN_TRACK_AGE_FRAMES.
        Otherwise a stranger walking into the FOV during cold start would
        be mistaken for the operator."""
        f = TargetFuser()
        # No prior fuse call → never had a lock.
        out = f.fuse(
            _decision(bearing_deg=0.0, confidence=0.9),
            0.05,
            _tracks(_track(tid="stranger", bearing_deg=0.0, age_frames=0)),
            0.05,
        )
        assert out is not None
        self.assertEqual(out.bearing_source, "uwb")
        self.assertIsNone(out.vision_track_id)


class TestTrackingStateMapping(unittest.TestCase):
    def test_predicting_promoted_when_vision_present(self):
        f = TargetFuser()
        d = _decision(state="PREDICTING", confidence=0.4)
        t = _track(age_frames=30)
        out = f.fuse(d, 0.05, _tracks(t), 0.05)
        assert out is not None
        self.assertEqual(out.tracking_state, "TRACKING")

    def test_predicting_stays_when_no_vision(self):
        f = TargetFuser()
        d = _decision(state="PREDICTING", confidence=0.4)
        out = f.fuse(d, 0.05, None, None)
        assert out is not None
        self.assertEqual(out.tracking_state, "PREDICTING")


class TestVelocityFallback(unittest.TestCase):
    """When UWB doesn't supply rates, vision history is differentiated."""

    def test_vision_only_velocity_finite_difference(self):
        """When UWB rates are absent, the vision-history finite difference
        gives a reasonable target velocity. Lock the track first via a
        healthy UWB tick, then go vision-only and check the differentiator."""
        f = TargetFuser()
        with mock.patch("brain.fusion.time.monotonic") as fake:
            # Tick 0 — both healthy at bearing 0° to lock track id.
            fake.return_value = 100.0
            f.fuse(
                _decision(bearing_deg=0.0, confidence=0.9),
                0.05,
                _tracks(_track(tid="1", bearing_deg=0.0, distance_m=2.0)),
                0.05,
            )
            # Tick 1 — UWB drops; vision still sees track 1, same place.
            fake.return_value = 100.3
            f.fuse(
                _decision(state="LOST"),
                0.05,
                _tracks(_track(tid="1", bearing_deg=0.0, distance_m=2.0, age_frames=35)),
                0.05,
            )
            # Tick 2 — vision shows the operator has moved leftward.
            fake.return_value = 100.6
            out = f.fuse(
                _decision(state="LOST"),
                0.05,
                _tracks(_track(tid="1", bearing_deg=10.0, distance_m=2.0, age_frames=40)),
                0.05,
            )
        assert out is not None
        self.assertEqual(out.bearing_source, "vision")
        # +ve lateral_rate = target moving to dog's left (body-y up).
        # Bearing going 0° → +10° means moving left, so target_vy_body > 0.
        self.assertGreater(out.target_vy_body, 0)


class TestFollowable(unittest.TestCase):
    def test_low_confidence_not_followable(self):
        f = TargetFuser()
        d = _decision(confidence=0.3)
        out = f.fuse(d, 0.05, None, None)
        assert out is not None
        self.assertFalse(out.is_followable)

    def test_predicting_confidence_floor_is_followable(self):
        """The UWB filter sets PREDICTING confidence to 0.4 — exactly the
        FOLLOWABLE_CONFIDENCE floor. PREDICTING must stay followable so a
        brief Mahalanobis-rejected sample doesn't kick FOLLOWING into
        RECOVERING. Regression guard for the 0.5 → 0.4 threshold change."""
        f = TargetFuser()
        d = _decision(state="PREDICTING", confidence=0.4)
        out = f.fuse(d, 0.05, None, None)
        assert out is not None
        self.assertEqual(out.tracking_state, "PREDICTING")
        self.assertTrue(out.is_followable)

    def test_old_sample_not_followable(self):
        f = TargetFuser()
        d = _decision(confidence=0.95)
        out = f.fuse(d, 0.5, None, None)  # 500 ms stale
        assert out is not None
        self.assertFalse(out.is_followable)


if __name__ == "__main__":
    unittest.main()
