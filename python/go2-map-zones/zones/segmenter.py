#!/usr/bin/env python3
"""segmenter -- split an occupancy grid into topological ZONES (rooms / coherent open regions).

The whole segmentation algorithm is pure NumPy + OpenCV, so it runs offline against any saved
occupancy map — no ROS, no live robot. Two loaders read a map off disk:

  - load_npz(path)              -- an {grid, res, origin_x, origin_y} archive
  - load_nav2_map(pgm, yaml)    -- a standard map_saver pgm+yaml pair

Both return the same tuple: (grid int16 HxW [-1 unknown / 0 free / 100 occupied], res, ox, oy).

Pipeline:
  1. OBSTACLE-ISLAND FILL: free-standing props (drums, pallets, racks, boxes, chairs, ...) are
     OCCUPIED blobs that are NOT part of the building's wall skeleton. Each such island used to
     puncture the free space and split one room into several spurious cores. We reclassify every
     small free-standing occupied island (footprint < island_max_m2, and not spanning >
     wall_span_m -- i.e. not a wall run) as FREE before segmenting. The one big connected wall
     structure is never touched.
  2. distance transform -> threshold to ROOM CORES (open interiors; narrow corridors/doorways
     fall below the threshold) -> connected components = seeds -> watershed floods each seed
     across free space, meeting at walls and mid-doorway -> one label per region.
  3. Each zone -> {id, center, polygon, area, nav_point, label} in MAP coordinates. `nav_point`
     is the deepest ORIGINALLY-free point of the zone (max distance-transform of the un-filled
     free mask), so a navigation goal lands in open space, never on the raw centroid (which can
     sit on a prop) or on a filled island. `label` is a generic quadrant tag (e.g. "NW",
     "center") for reports/markers only.

NOTE on merging: we deliberately do NOT auto-merge adjacent regions. On open-plan layouts there
is no robust LOCAL geometric criterion to tell "intra-room split" from "room opening onto
corridor". So the invariant we keep is: never emit a mixed-room zone. A room split into two
coherent halves is only scanned slightly redundantly, never incorrectly.
"""

import json
import os

import cv2
import numpy as np


def _island_fill(grid, res, island_max_m2=2.5, wall_span_m=3.0):
    """Return a free mask with small free-standing OCCUPIED islands reclassified as free. The single
    largest connected occupied component (the building wall skeleton) is always kept; any other occupied
    component is kept only if it is large (>= island_max_m2) OR spans > wall_span_m in either axis
    (likely a wall run on a noisy map). Everything else is swallowed into free."""
    free = (grid == 0).astype(np.uint8)
    occ = (grid == 100).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(occ, 8)
    if n <= 1:
        return free, 0
    wall_lab = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))  # the one big wall structure
    free_clean = free.copy()
    filled = 0
    for l in range(1, n):
        if l == wall_lab:
            continue
        area_m2 = stats[l, cv2.CC_STAT_AREA] * res * res
        span = max(stats[l, cv2.CC_STAT_WIDTH], stats[l, cv2.CC_STAT_HEIGHT]) * res
        if area_m2 < island_max_m2 and span < wall_span_m:
            free_clean[lab == l] = 1  # free-standing prop -> treat as free
            filled += 1
    return free_clean, filled


def _quadrant_label(cx, cy, ox, oy, W, H, res):
    """Generic, world-agnostic location tag from the centroid's third within the map extent."""
    fx = (cx - ox) / (W * res)
    fy = (cy - oy) / (H * res)
    ns = "S" if fy < 1 / 3 else ("N" if fy > 2 / 3 else "")
    we = "W" if fx < 1 / 3 else ("E" if fx > 2 / 3 else "")
    return (ns + we) or "center"


def segment_grid(
    grid, res, ox, oy, core_dist_m=1.6, min_area_m2=6.0, island_max_m2=2.5, wall_span_m=3.0
):
    """grid: int16 HxW (-1 unknown / 0 free / 100 occupied). Returns (zones, label_markers)."""
    H, W = grid.shape
    free_clean, _ = _island_fill(grid, res, island_max_m2, wall_span_m)
    # distance transform of the CLEANED free mask drives cores + watershed (props no longer split rooms);
    # distance transform of the ORIGINAL free mask drives nav_point (so the goal avoids real props too).
    dist = cv2.distanceTransform(free_clean, cv2.DIST_L2, 5)
    dist_orig = cv2.distanceTransform((grid == 0).astype(np.uint8), cv2.DIST_L2, 5)
    cores = (dist > core_dist_m / res).astype(np.uint8)  # open region interiors
    ncores, seeds = cv2.connectedComponents(cores)  # 0=bg, 1..ncores-1 = a core
    markers = np.zeros((H, W), np.int32)
    markers[free_clean == 0] = 1  # walls/unknown = barrier (background marker)
    markers[seeds > 0] = seeds[seeds > 0] + 1  # region cores -> labels >= 2
    cv2.watershed(
        cv2.cvtColor(free_clean * 255, cv2.COLOR_GRAY2BGR), markers
    )  # flood free space from cores
    zones = []
    for lab in range(2, int(markers.max()) + 1):
        m = (markers == lab).astype(np.uint8)
        area = float(m.sum()) * res * res
        if area < min_area_m2:
            continue
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            continue
        c = max(cnts, key=cv2.contourArea)
        poly = cv2.approxPolyDP(c, 0.12 / res, True).reshape(-1, 2)
        M = cv2.moments(m, binaryImage=True)
        cx_px, cy_px = M["m10"] / M["m00"], M["m01"] / M["m00"]
        # nav_point = deepest ORIGINALLY-free point inside this zone (open space, off walls AND props)
        np_idx = np.unravel_index(np.argmax(dist_orig * m), dist_orig.shape)
        cx_w, cy_w = round(float(ox + cx_px * res), 3), round(float(oy + cy_px * res), 3)
        zones.append(
            {
                "center": [cx_w, cy_w],
                "nav_point": [
                    round(float(ox + np_idx[1] * res), 3),
                    round(float(oy + np_idx[0] * res), 3),
                ],
                "polygon": [[round(ox + px * res, 3), round(oy + py * res, 3)] for px, py in poly],
                "area": round(area, 2),
                "label": _quadrant_label(cx_w, cy_w, ox, oy, W, H, res),
                "_label": lab,
            }
        )
    zones.sort(key=lambda z: -z["area"])
    for i, z in enumerate(zones):
        z["id"] = f"zone_{i}"
    return zones, markers


def _viz(grid, markers, zones, res, ox, oy, path):
    """Write a colour PNG: free=light, occupied=black, one random colour per zone, watershed lines
    black, then flip so +Y is up and draw upright zone labels + nav_point crosses."""
    H, W = grid.shape
    rng = np.random.default_rng(0)
    out = np.full((H, W, 3), 60, np.uint8)
    out[grid == 0] = (230, 230, 230)
    out[grid == 100] = (0, 0, 0)
    for z in zones:
        col = tuple(int(c) for c in rng.integers(60, 255, 3))
        out[markers == z["_label"]] = col
    out[markers == -1] = (0, 0, 0)  # watershed lines
    out = cv2.flip(out, 0)  # flip so +Y is up, THEN draw labels (upright)
    for z in zones:
        cx = int((z["center"][0] - ox) / res)
        cy = H - 1 - int((z["center"][1] - oy) / res)
        cv2.circle(out, (cx, cy), 5, (0, 0, 255), -1)
        cv2.putText(
            out,
            z["id"].replace("zone_", "Z") + ":" + z.get("label", ""),
            (cx - 14, cy - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 0, 255),
            2,
        )
        nx = int((z["nav_point"][0] - ox) / res)
        ny = H - 1 - int((z["nav_point"][1] - oy) / res)
        cv2.drawMarker(out, (nx, ny), (255, 0, 255), cv2.MARKER_CROSS, 12, 2)  # nav_point
    cv2.imwrite(path, out)


# ------------------------------------------------------------------ map loaders


def load_npz(path):
    """Load our own occupancy archive -> (grid int16, res, ox, oy).

    Expects arrays: grid (int16 HxW, -1/0/100), res (float, m/cell), origin_x, origin_y (floats)."""
    d = np.load(path)
    grid = np.asarray(d["grid"], dtype=np.int16)
    return grid, float(d["res"]), float(d["origin_x"]), float(d["origin_y"])


def _parse_map_yaml(path):
    """Minimal parser for the map_saver yaml (image / resolution / origin / negate) -> dict, so we
    don't need a yaml dependency for a handful of scalar fields."""
    d = {}
    with open(path) as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            d[k.strip()] = v.strip()
    res = float(d["resolution"])
    origin = (
        json.loads(d["origin"])
        if d["origin"].startswith("[")
        else [float(x) for x in d["origin"].split()]
    )
    return d, res, float(origin[0]), float(origin[1])


def load_nav2_map(pgm_path, yaml_path):
    """Load a standard nav2 map_saver pair -> (grid int16, res, ox, oy).

    The pgm carries pixel intensities (map_saver default, negate 0): free≈254, occupied≈0,
    unknown≈205. We threshold those back into the occupancy convention the segmenter expects
    (-1 unknown / 0 free / 100 occupied). The pgm stores row 0 = top (max y); the segmenter and
    its viz work in that same image frame, so we keep the raw orientation — the world origin
    (bottom-left) is applied through `res`/`origin` exactly as it is for the .npz path."""
    d, res, ox, oy = _parse_map_yaml(yaml_path)
    gray = cv2.imread(pgm_path, cv2.IMREAD_GRAYSCALE)
    if gray is None:
        raise FileNotFoundError(f"map image not found or unreadable: {pgm_path}")
    # `occupied_thresh` / `free_thresh` in the yaml are fractions of full white; fall back to the
    # nav2 defaults if the yaml omits them.
    occ_thresh = float(d.get("occupied_thresh", 0.65))
    free_thresh = float(d.get("free_thresh", 0.196))
    p = gray.astype(np.float32) / 255.0
    # map_saver (negate 0) writes darker = more occupied, so occupancy = 1 - intensity.
    occ = 1.0 - p
    grid = np.full(gray.shape, -1, dtype=np.int16)  # unknown by default
    grid[occ >= occ_thresh] = 100  # occupied
    grid[occ <= free_thresh] = 0  # free
    return grid, res, ox, oy
