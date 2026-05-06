"""End-to-end test of the LIDAR → free_space → safety wrapper contract.

Watchtower's `go2_lidar_filter.py` publishes JSON on
`/go2/perception/free_space`; the brain reads it via `FreeSpace.from_json`,
queries `min_distance_in_cone` for each translational axis, and clips
the FSM's velocity command. This test exercises the **whole contract**
end-to-end from a synthetic JSON payload (matching the watchtower wire
format byte-for-byte) all the way through to the inner controller's
clipped command.

Why this exists: the per-side unit tests catch local bugs but miss
schema/coordinate disagreements between repos. If watchtower starts
publishing `bearing_origin: "forward"` or renames `distances_m`, the
brain unit tests still pass — only this one fails. So this is the
load-bearing test for the brain↔watchtower interface.

If you change the published JSON shape on the watchtower side
(`go2-Watchtower/go2_lidar_filter.py`), update `_emit_free_space_json`
below to match — that's the contract.
"""

from __future__ import annotations

import json
import math
import unittest
from typing import Optional

from brain.models import FreeSpace
from brain.safety import SafetyController


# ---------------------------------------------------------------------------
# Watchtower wire-format reproduction
# ---------------------------------------------------------------------------

SECTOR_DEG = 10.0
N_SECTORS = 36
RANGE_MAX_M = 5.0


def _bin_index(bearing_deg: float) -> int:
    """Same bin math as watchtower's `_scan_to_sectors`: idx i covers
    [-180 + i*sector_deg, -180 + (i+1)*sector_deg). Centered convention."""
    idx = int((bearing_deg + 180.0) / SECTOR_DEG)
    return max(0, min(N_SECTORS - 1, idx))


def _emit_free_space_json(
    obstacles: list[tuple[float, float]],
) -> str:
    """Build a JSON payload byte-for-byte equivalent to what
    `go2_lidar_filter.py` publishes. Each `obstacles` entry is
    `(bearing_deg, range_m)` — bearing in REP-103 (+ left, 0 = ahead)."""
    distances = [RANGE_MAX_M] * N_SECTORS
    for bearing_deg, rng in obstacles:
        i = _bin_index(bearing_deg)
        if rng < distances[i]:
            distances[i] = rng
    payload = {
        "stamp_ns": 1234567890,
        "sector_deg": SECTOR_DEG,
        "max_range_m": RANGE_MAX_M,
        "distances_m": [round(float(d), 3) for d in distances],
        "bearing_origin": "centered",
    }
    return json.dumps(payload, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Shared fakes (same shape as the brain's real Subscriber)
# ---------------------------------------------------------------------------


class FakeFreeSpaceSrc:
    def __init__(self, fs: Optional[FreeSpace], age_s: float = 0.05):
        self._fs = fs
        self._age = age_s if fs is not None else None

    def latest(self): return self._fs
    def age_s(self): return self._age


class FakeInner:
    def __init__(self):
        self.last: Optional[tuple] = None

    def set_velocity(self, vx: float = 0.0, vy: float = 0.0, vyaw: float = 0.0):
        self.last = (vx, vy, vyaw)

    def stop(self): pass


def _drive(json_str: str, vx: float = 0.0, vy: float = 0.0, vyaw: float = 0.0):
    """Parse JSON, run through SafetyController, return (out_tuple, status)."""
    fs = FreeSpace.from_json(json_str)
    inner = FakeInner()
    sc = SafetyController(
        inner, FakeFreeSpaceSrc(fs),
        min_distance_m=0.40, ramp_m=0.30, cone_half_deg=30.0, strict=False,
    )
    sc.set_velocity(vx=vx, vy=vy, vyaw=vyaw)
    return inner.last, sc.status()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSchemaRoundTrip(unittest.TestCase):
    """The JSON produced by watchtower must parse cleanly on the brain side."""

    def test_empty_world(self):
        fs = FreeSpace.from_json(_emit_free_space_json([]))
        self.assertEqual(fs.sector_deg, SECTOR_DEG)
        self.assertEqual(fs.max_range_m, RANGE_MAX_M)
        self.assertEqual(len(fs.distances_m), N_SECTORS)
        self.assertEqual(fs.bearing_origin, "centered")
        self.assertTrue(all(d == RANGE_MAX_M for d in fs.distances_m))

    def test_obstacle_lookup(self):
        # Wall directly ahead at 0.5 m. Brain queries forward cone (0°±30°)
        # and should find this in one of the bins inside that cone.
        fs = FreeSpace.from_json(_emit_free_space_json([(0.0, 0.5)]))
        self.assertEqual(fs.min_distance_in_cone(0.0, 30.0), 0.5)
        # Same obstacle at side bearings — outside front cone.
        self.assertEqual(fs.min_distance_in_cone(90.0, 30.0), RANGE_MAX_M)
        self.assertEqual(fs.min_distance_in_cone(180.0, 30.0), RANGE_MAX_M)


class TestForwardClip(unittest.TestCase):
    def test_clear_path_unchanged(self):
        out, st = _drive(_emit_free_space_json([]), vx=0.30, vyaw=0.5)
        self.assertEqual(out, (0.30, 0.0, 0.5))
        self.assertFalse(st.clipped)
        self.assertEqual(st.min_ahead_m, RANGE_MAX_M)

    def test_wall_ahead_ramps_velocity(self):
        # Linear ramp: scale = (range - min) / ramp_m
        # range=0.5, min=0.4, ramp=0.3 → scale = 0.333 → vx = 0.10
        out, st = _drive(_emit_free_space_json([(0.0, 0.5)]), vx=0.30)
        self.assertAlmostEqual(out[0], 0.10, places=4)
        self.assertAlmostEqual(st.scale_vx, 1.0 / 3.0, places=4)
        self.assertTrue(st.clipped)

    def test_wall_inside_min_full_clip(self):
        out, st = _drive(_emit_free_space_json([(0.0, 0.35)]), vx=0.30)
        self.assertEqual(out[0], 0.0)
        self.assertEqual(st.scale_vx, 0.0)

    def test_wall_past_ramp_no_clip(self):
        out, st = _drive(_emit_free_space_json([(0.0, 1.0)]), vx=0.30)
        self.assertAlmostEqual(out[0], 0.30, places=6)
        self.assertFalse(st.clipped)


class TestDirectionalIndependence(unittest.TestCase):
    def test_wall_ahead_doesnt_block_backoff(self):
        out, st = _drive(_emit_free_space_json([(0.0, 0.30)]), vx=-0.30)
        self.assertEqual(out[0], -0.30)
        self.assertFalse(st.clipped)

    def test_wall_behind_doesnt_block_advance(self):
        out, st = _drive(_emit_free_space_json([(180.0, 0.30)]), vx=+0.30)
        self.assertEqual(out[0], 0.30)

    def test_wall_behind_blocks_backoff(self):
        # Bearing 180° is the rear cone direction for -vx.
        out, st = _drive(_emit_free_space_json([(180.0, 0.50)]), vx=-0.30)
        self.assertAlmostEqual(out[0], -0.10, places=4)

    def test_wall_left_blocks_only_left_strafe(self):
        # +90° is the +vy cone (left). Strafe left → clip; forward → free.
        out_y, _ = _drive(_emit_free_space_json([(90.0, 0.50)]), vy=+0.30)
        self.assertAlmostEqual(out_y[1], 0.10, places=4)
        out_x, _ = _drive(_emit_free_space_json([(90.0, 0.50)]), vx=+0.30)
        self.assertEqual(out_x[0], 0.30)


class TestYawPassthrough(unittest.TestCase):
    def test_yaw_passes_when_surrounded(self):
        # Wall in every sector at 0.30 m → vx, vy zeroed but vyaw untouched.
        all_close = [(b, 0.30) for b in range(-180, 180, 10)]
        out, st = _drive(_emit_free_space_json(all_close),
                         vx=0.50, vy=0.50, vyaw=0.70)
        self.assertEqual(out, (0.0, 0.0, 0.70))


class TestEdgeCases(unittest.TestCase):
    def test_wraparound_at_back_seam(self):
        # Bearing of -179° lands in bin 0 (or thereabouts); +179° in bin 35.
        # Both must be reachable from a -180° rear-cone query.
        for sign in (-1, 1):
            obs_bearing = sign * 179.0
            fs = FreeSpace.from_json(_emit_free_space_json([(obs_bearing, 0.50)]))
            min_d = fs.min_distance_in_cone(180.0, 30.0)
            self.assertEqual(
                min_d, 0.50,
                f"bearing {obs_bearing}° not reachable from rear-cone query",
            )

    def test_quantization_at_cone_edge(self):
        """Documents a real bin-vs-cone boundary effect.

        Watchtower bins right-exclusive: bin 21 covers [+30°, +40°),
        center +35°. Brain's cone-membership check uses center-only
        (`abs(center - bearing) <= half_angle`), so bin 21's +35° center
        is outside a forward 0°±30° cone (35 > 30) → not queried.

        Practical impact: a point at exactly the cone edge gets binned
        into the next sector out and is missed by the safety query. For
        real obstacles (walls, not single points), this is harmless —
        adjacent sectors at +25° and within will fire. But know this
        when picking `cone_half_deg`: the *effective* half-angle is
        ⌊half_angle / sector_deg⌋ × sector_deg + sector_deg/2 in the
        worst case (i.e., ~25° for half=30° / sec=10°)."""
        out_edge, _ = _drive(_emit_free_space_json([(30.0, 0.30)]), vx=+0.30)
        self.assertEqual(out_edge[0], 0.30, "edge point misses by quantization")

        # 1° inside the edge → does fire.
        out_inside, _ = _drive(_emit_free_space_json([(29.0, 0.30)]), vx=+0.30)
        self.assertEqual(out_inside[0], 0.0, "point inside edge clips")


class TestStaleFreeSpace(unittest.TestCase):
    def test_no_payload_passthrough(self):
        inner = FakeInner()
        sc = SafetyController(
            inner, FakeFreeSpaceSrc(None),
            min_distance_m=0.40, ramp_m=0.30, cone_half_deg=30.0, strict=False,
        )
        sc.set_velocity(vx=0.30, vyaw=0.5)
        self.assertEqual(inner.last, (0.30, 0.0, 0.5))


if __name__ == "__main__":
    unittest.main()
