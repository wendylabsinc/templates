"""Smoke tests for the SafetyController velocity clipper.

Pure-Python: no DDS, no controllers — `FakeFreeSpace` mimics the
`FreeSpaceSubscriber` `latest()`/`age_s()` interface, `FakeInner`
records every set_velocity/stop call so we can assert what reached the
wrapped controller.

What we cover
-------------
- Pass-through with a clear path.
- Full clip when an obstacle sits inside `min_distance_m`.
- Linear ramp between `min_distance_m` and `min_distance_m + ramp_m`.
- Yaw is never clipped (rotation in place is always safe).
- `stop()` is never clipped (emergency must always reach the inner).
- Back-off (-vx) is unaffected when the wall is ahead.
- Missing free_space: pass-through by default, fail-safe in strict mode.
- Telemetry: status snapshot reflects the clip decision and the
  forward-cone min distance even at HOLD.
"""

from __future__ import annotations

import unittest
from typing import Optional

from brain.models import FreeSpace
from brain.safety import SafetyController, SafetyStatus


class FakeFreeSpace:
    """Mimics FreeSpaceSubscriber's read interface."""

    def __init__(self) -> None:
        self._fs: Optional[FreeSpace] = None
        self._age: Optional[float] = None

    def set(self, fs: Optional[FreeSpace], age_s: float = 0.05) -> None:
        self._fs = fs
        self._age = age_s if fs is not None else None

    def latest(self) -> Optional[FreeSpace]:
        return self._fs

    def age_s(self) -> Optional[float]:
        return self._age


class FakeInner:
    """Records calls so tests can assert what the wrapper actually sent."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def set_velocity(self, vx: float = 0.0, vy: float = 0.0, vyaw: float = 0.0) -> None:
        self.calls.append(("set", vx, vy, vyaw))

    def stop(self) -> None:
        self.calls.append(("stop",))


def _clear_fs(max_range: float = 5.0) -> FreeSpace:
    """All 36 sectors at max_range — nothing in the way."""
    return FreeSpace(
        sector_deg=10.0,
        max_range_m=max_range,
        distances_m=[max_range] * 36,
        bearing_origin="centered",
    )


def _wall_ahead(distance: float, max_range: float = 5.0) -> FreeSpace:
    """All clear except the two sectors straddling 0° bearing."""
    distances = [max_range] * 36
    # Centered convention: sector i center = -180 + (i+0.5)*10.
    # Sector 17 center = -8°, sector 18 center = +2° — both inside ±10°.
    distances[17] = distance
    distances[18] = distance
    return FreeSpace(
        sector_deg=10.0,
        max_range_m=max_range,
        distances_m=distances,
        bearing_origin="centered",
    )


def _safety(
    inner: FakeInner,
    fs: FakeFreeSpace,
    *,
    strict: bool = False,
) -> SafetyController:
    """Construct with explicit defaults so tests don't depend on env vars."""
    return SafetyController(
        inner, fs,
        min_distance_m=0.40,
        ramp_m=0.30,
        cone_half_deg=30.0,
        strict=strict,
    )


class TestPassthrough(unittest.TestCase):
    def test_clear_path_unchanged(self):
        inner, fs = FakeInner(), FakeFreeSpace()
        fs.set(_clear_fs())
        sc = _safety(inner, fs)
        sc.set_velocity(vx=0.30, vy=0.10, vyaw=0.20)
        self.assertEqual(inner.calls[-1], ("set", 0.30, 0.10, 0.20))
        self.assertFalse(sc.status().clipped)
        self.assertEqual(sc.status().min_ahead_m, 5.0)

    def test_hold_does_not_clip_but_reports_ahead(self):
        inner, fs = FakeInner(), FakeFreeSpace()
        fs.set(_wall_ahead(1.5))
        sc = _safety(inner, fs)
        sc.set_velocity(vx=0.0, vy=0.0, vyaw=0.0)
        self.assertEqual(inner.calls[-1], ("set", 0.0, 0.0, 0.0))
        self.assertFalse(sc.status().clipped)
        # Telemetry still reports the front-cone min — operators want
        # a continuous "obstacle ahead" readout, not just when moving.
        self.assertEqual(sc.status().min_ahead_m, 1.5)


class TestRampClip(unittest.TestCase):
    def test_full_clip_inside_min(self):
        inner, fs = FakeInner(), FakeFreeSpace()
        fs.set(_wall_ahead(0.30))   # < min_distance_m (0.40)
        sc = _safety(inner, fs)
        sc.set_velocity(vx=0.30, vy=0, vyaw=0)
        self.assertEqual(inner.calls[-1][1], 0.0)   # vx fully clipped
        self.assertEqual(sc.status().scale_vx, 0.0)
        self.assertTrue(sc.status().clipped)

    def test_no_clip_past_ramp(self):
        inner, fs = FakeInner(), FakeFreeSpace()
        fs.set(_wall_ahead(0.80))   # > min + ramp (0.70)
        sc = _safety(inner, fs)
        sc.set_velocity(vx=0.30, vy=0, vyaw=0)
        self.assertAlmostEqual(inner.calls[-1][1], 0.30)
        self.assertAlmostEqual(sc.status().scale_vx, 1.0)
        self.assertFalse(sc.status().clipped)

    def test_linear_ramp_at_midpoint(self):
        # min=0.40, ramp=0.30 → top=0.70. Halfway = 0.55 → scale=0.5.
        inner, fs = FakeInner(), FakeFreeSpace()
        fs.set(_wall_ahead(0.55))
        sc = _safety(inner, fs)
        sc.set_velocity(vx=0.30, vy=0, vyaw=0)
        self.assertAlmostEqual(inner.calls[-1][1], 0.15, places=4)
        self.assertAlmostEqual(sc.status().scale_vx, 0.5, places=4)


class TestAxisIndependence(unittest.TestCase):
    def test_yaw_never_clipped_even_when_surrounded(self):
        inner, fs = FakeInner(), FakeFreeSpace()
        # Every sector dangerously close → both vx and vy must zero.
        fs.set(FreeSpace(
            sector_deg=10.0, max_range_m=5.0,
            distances_m=[0.10] * 36, bearing_origin="centered",
        ))
        sc = _safety(inner, fs)
        sc.set_velocity(vx=0.50, vy=0.50, vyaw=0.70)
        self.assertEqual(inner.calls[-1], ("set", 0.0, 0.0, 0.70))

    def test_back_off_unaffected_when_wall_is_ahead(self):
        inner, fs = FakeInner(), FakeFreeSpace()
        fs.set(_wall_ahead(0.30))   # only the front cone is blocked
        sc = _safety(inner, fs)
        sc.set_velocity(vx=-0.30, vy=0, vyaw=0)
        self.assertEqual(inner.calls[-1][1], -0.30)
        self.assertFalse(sc.status().clipped)


class TestStop(unittest.TestCase):
    def test_stop_passes_through_unconditionally(self):
        inner, fs = FakeInner(), FakeFreeSpace()
        # Even in strict mode with no free_space, stop must reach inner.
        sc = _safety(inner, fs, strict=True)
        sc.stop()
        self.assertEqual(inner.calls[-1], ("stop",))


class TestMissingFreeSpace(unittest.TestCase):
    def test_passthrough_by_default(self):
        inner, fs = FakeInner(), FakeFreeSpace()
        # fs.latest() returns None — never publish.
        sc = _safety(inner, fs, strict=False)
        sc.set_velocity(vx=0.30, vy=0.10, vyaw=0.20)
        self.assertEqual(inner.calls[-1], ("set", 0.30, 0.10, 0.20))
        self.assertFalse(sc.status().clipped)
        self.assertFalse(sc.status().strict_blocked)

    def test_strict_mode_zeros_translation(self):
        inner, fs = FakeInner(), FakeFreeSpace()
        sc = _safety(inner, fs, strict=True)
        sc.set_velocity(vx=0.30, vy=0.10, vyaw=0.20)
        # Strict + missing free_space → vx,vy=0; yaw still passes.
        self.assertEqual(inner.calls[-1], ("set", 0.0, 0.0, 0.20))
        self.assertTrue(sc.status().clipped)
        self.assertTrue(sc.status().strict_blocked)


class TestStatusSnapshot(unittest.TestCase):
    def test_status_fields_populated(self):
        inner, fs = FakeInner(), FakeFreeSpace()
        fs.set(_wall_ahead(0.50), age_s=0.12)
        sc = _safety(inner, fs)
        sc.set_velocity(vx=0.30, vy=0, vyaw=0)
        s = sc.status()
        self.assertIsInstance(s, SafetyStatus)
        self.assertEqual(s.free_space_age_s, 0.12)
        self.assertEqual(s.min_ahead_m, 0.50)
        self.assertGreater(s.scale_vx, 0.0)
        self.assertLess(s.scale_vx, 1.0)
        self.assertEqual(s.scale_vy, 1.0)   # vy=0 means no clip on that axis
        self.assertTrue(s.clipped)
        self.assertFalse(s.strict_blocked)


if __name__ == "__main__":
    unittest.main()
