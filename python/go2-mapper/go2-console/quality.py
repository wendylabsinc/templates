#!/usr/bin/env python3
"""
quality.py — score an exported map so you know it's G1-worthy BEFORE
deploying: a bad map costs a re-walk now or a lost humanoid later.

Metrics (all from artifacts the export already produces):

  wall_crispness   Erode the occupied mask once (3x3). Crisp 1-cell walls
                   vanish; drift-smeared walls survive erosion. Score =
                   fraction of occupied cells surviving. High = doubled
                   walls = odometry drift during the walk.
  floor_inlier_ratio  RANSAC floor inliers / points in the floor band.
                   Low = tilted or fragmented floor fit.
  coverage_ratio   free / (free + unknown) inside the map's bounding hull
                   proxy. Low = you didn't walk enough of the space.
  density_per_m2   map points per m² of free space. Low = sparse map,
                   weak localization prior for the G1.

Verdict: fail if any hard threshold trips, warn on soft ones, else pass.
Thresholds are deliberately conservative for a home-scale single floor.
"""

import numpy as np

# soft, hard thresholds
THRESH = {
    "wall_crispness":     {"warn": 0.10, "fail": 0.30},   # higher is worse; calibrated: crisp map ~0.0, fully drift-doubled walls ~0.20
    "floor_inlier_ratio": {"warn": 0.55, "fail": 0.30},   # lower is worse
    "coverage_ratio":     {"warn": 0.45, "fail": 0.20},   # lower is worse
    "density_per_m2":     {"warn": 800,  "fail": 200},    # lower is worse
}


def _shift_or(mask: np.ndarray, radius: int) -> np.ndarray:
    """Binary dilation with a (2r+1)² kernel via shifted ORs."""
    out = mask.copy()
    for dr in range(-radius, radius + 1):
        for dc in range(-radius, radius + 1):
            if dr == 0 and dc == 0:
                continue
            shifted = np.zeros_like(mask)
            rs = slice(max(dr, 0), mask.shape[0] + min(dr, 0))
            rd = slice(max(-dr, 0), mask.shape[0] + min(-dr, 0))
            cs = slice(max(dc, 0), mask.shape[1] + min(dc, 0))
            cd = slice(max(-dc, 0), mask.shape[1] + min(-dc, 0))
            shifted[rd, cd] = mask[rs, cs]
            out |= shifted
    return out


def _erode(mask: np.ndarray, radius: int = 1) -> np.ndarray:
    """Binary erosion = complement of dilation of the complement."""
    return ~_shift_or(~mask, radius)


def wall_crispness(occupied: np.ndarray, resolution_m: float) -> float:
    """Detect drift-doubled/smeared walls.

    Plain erosion misses drift's signature failure: two crisp PARALLEL
    copies of the same wall a few cells apart. Morphological closing with
    a ~20 cm radius merges such doubles into one thick blob; a crisp
    single wall closes back to itself. Detection floor: doubling below
    ~15 cm stays invisible on a 5 cm grid — acceptable, since Nav2's
    costmap inflation absorbs offsets that small. Score = fraction of the closed
    mask surviving a 2-cell erosion — thick merged walls survive, true
    1-2 cell walls don't.
    """
    if not occupied.any():
        return 0.0
    r_close = max(1, int(round(0.20 / resolution_m)))  # 20 cm merge radius
    closed = _erode(_shift_or(occupied, r_close), r_close)
    survived = _erode(closed, 2)
    return float(survived.sum() / max(closed.sum(), 1))


def assess(
    grid_img: np.ndarray,          # PGM pixels, image row order (row 0 = top)
    resolution_m: float,
    aligned_xyz: np.ndarray,       # floor-aligned cloud
    floor_inliers: int,
    lidar_height_m: float,
) -> dict:
    occupied = grid_img == 0
    free = grid_img == 254
    n_occ, n_free = int(occupied.sum()), int(free.sum())

    # wall crispness: drift-doubled walls survive close+erode, crisp ones don't
    crisp = wall_crispness(occupied, resolution_m)

    # floor inlier ratio against points near the (now aligned) floor
    floor_band = int(((aligned_xyz[:, 2] > -0.10) & (aligned_xyz[:, 2] < 0.10)).sum())
    inlier_ratio = float(floor_inliers / floor_band) if floor_band else 0.0

    n_unknown_inside = int((grid_img == 205).sum())
    coverage = float(n_free / (n_free + n_unknown_inside)) if (n_free + n_unknown_inside) else 0.0

    free_m2 = n_free * resolution_m ** 2
    density = float(aligned_xyz.shape[0] / free_m2) if free_m2 > 0 else 0.0

    metrics = {
        "wall_crispness": round(crisp, 3),
        "floor_inlier_ratio": round(inlier_ratio, 3),
        "coverage_ratio": round(coverage, 3),
        "density_per_m2": round(density, 1),
    }

    issues = []
    verdict = "pass"
    for name, val in metrics.items():
        t = THRESH[name]
        worse_is_higher = name == "wall_crispness"
        failed = val > t["fail"] if worse_is_higher else val < t["fail"]
        warned = val > t["warn"] if worse_is_higher else val < t["warn"]
        if failed:
            verdict = "fail"
            issues.append(f"{name}={val} beyond fail threshold {t['fail']}")
        elif warned and verdict != "fail":
            verdict = "warn"
            issues.append(f"{name}={val} beyond warn threshold {t['warn']}")

    hints = {
        "wall_crispness": "doubled/smeared walls -> odometry drift: re-walk slower, close the loop back to start",
        "floor_inlier_ratio": "weak floor fit: check LIDAR_HEIGHT_OFFSET_M, avoid starting on a rug",
        "coverage_ratio": "large unknown areas: walk the missing rooms, watch Foxglove for gaps",
        "density_per_m2": "sparse map: walk slower or longer for a stronger G1 localization prior",
    }
    return {
        "verdict": verdict,
        "metrics": metrics,
        "issues": issues,
        "hints": {k.split("=")[0]: hints[k.split("=")[0]] for k in
                  [i.split("=")[0] for i in issues]} if issues else {},
    }
