#!/usr/bin/env python3
"""
nogo.py — turn user-drawn polygons into a Nav2 keepout filter mask.

Input polygons arrive in MAP coordinates (meters, floor-aligned frame) from
the console's canvas editor. Output is keepout.pgm/.yaml with the SAME
dimensions/origin as the session's map.pgm, so Nav2's costmap_filters
consume it with zero G1-side custom code:

  keepout convention: 0 (black) = forbidden, 254 (white) = allowed.

Rasterization: vectorized even-odd (crossing number) point-in-polygon over
the grid — pure numpy, fast enough for home-scale grids (<1M cells).
"""

import numpy as np


def _points_in_polygon(xs: np.ndarray, ys: np.ndarray, poly: list) -> np.ndarray:
    """Even-odd rule for flat arrays of query points against one polygon."""
    inside = np.zeros(xs.shape[0], dtype=bool)
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        crosses = ((y1 > ys) != (y2 > ys)) & (
            xs < (x2 - x1) * (ys - y1) / (y2 - y1 + 1e-12) + x1
        )
        inside ^= crosses
    return inside


def build_keepout(
    polygons: list,        # [ {"name": str, "points": [[x,y], ...]}, ... ]
    width: int,
    height: int,
    resolution: float,
    origin: list,          # [ox, oy, 0]
) -> np.ndarray:
    """Return keepout grid in IMAGE row order (row 0 = top), PGM-ready."""
    keep = np.full((height, width), 254, dtype=np.uint8)  # allowed
    if not polygons:
        return keep
    # cell-center coordinates in map frame, ROS row order (row 0 = bottom)
    cols, rows = np.meshgrid(np.arange(width), np.arange(height))
    xs = origin[0] + (cols.ravel() + 0.5) * resolution
    ys = origin[1] + (rows.ravel() + 0.5) * resolution
    forbidden = np.zeros(xs.shape[0], dtype=bool)
    for poly in polygons:
        pts = poly["points"] if isinstance(poly, dict) else poly
        if len(pts) >= 3:
            forbidden |= _points_in_polygon(xs, ys, pts)
    ros_grid = np.full((height, width), 254, dtype=np.uint8)
    ros_grid.ravel()[forbidden] = 0
    return np.flipud(ros_grid)  # to image row order for PGM


def write_keepout_yaml(path: str, resolution: float, origin: list) -> None:
    with open(path, "w") as f:
        f.write(
            "image: keepout.pgm\n"
            f"resolution: {resolution}\n"
            f"origin: [{origin[0]:.4f}, {origin[1]:.4f}, 0.0]\n"
            "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\nmode: trinary\n"
        )
