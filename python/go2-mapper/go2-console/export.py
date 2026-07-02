#!/usr/bin/env python3
"""
export.py — turn a raw Point-LIO map into G1-ready navigation artifacts.

Input:  map.pcd in Point-LIO's map frame (gravity-aligned, origin at the
        Go2's SLAM start pose, z = lidar height at start, NOT the floor).

Output (per session, under EXPORT_DIR/<session>/):
  map.pcd        floor-aligned 3D cloud (z=0 exactly at the fitted floor)
                 -> G1 LiDAR localization (FAST-LIO-Localization / ICP)
  map.pgm        2D occupancy grid, sliced for the G1's body height
  map.yaml       Nav2 map_server metadata (origin, resolution)
  map_meta.json  provenance: slice bounds, floor-fit transform, date, source
  waypoints.json copied through if marked (door pose etc.), transformed into
                 the SAME floor-aligned frame so the G1 consumes it directly

Why floor alignment matters: without it, the map's z origin is wherever the
Go2's lidar happened to be at start (~0.4 m up, tilted by IMU init error).
The G1 stack assumes z=0 == floor; a mismatched origin silently offsets
your door waypoint and the localization prior.

Single-floor assumption (per project constraints): one global RANSAC plane
fit is enough — no per-region floor handling.
"""

import json
import os
import sys
import time

import numpy as np

import quality

# ---------------------------------------------------------------- PCD I/O


def read_pcd(path: str) -> np.ndarray:
    """Minimal reader for the binary xyz PCDs our viz_bridge writes."""
    with open(path, "rb") as f:
        header = {}
        while True:
            line = f.readline().decode(errors="replace").strip()
            key = line.split(" ")[0].upper()
            header[key] = line.split(" ")[1:]
            if key == "DATA":
                data_kind = header["DATA"][0]
                break
        n = int(header["POINTS"][0])
        if data_kind == "binary":
            buf = f.read(n * 12)
            return np.frombuffer(buf, dtype="<f4").reshape(n, 3).copy()
        # ascii fallback
        pts = np.loadtxt(f, dtype=np.float32, max_rows=n)
        return pts[:, :3]


def write_pcd(path: str, xyz: np.ndarray) -> None:
    header = (
        "# .PCD v0.7 - Point Cloud Data file format\n"
        "VERSION 0.7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\n"
        f"WIDTH {xyz.shape[0]}\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\n"
        f"POINTS {xyz.shape[0]}\nDATA binary\n"
    )
    with open(path, "wb") as f:
        f.write(header.encode())
        f.write(xyz.astype("<f4").tobytes())


# ------------------------------------------------------------- floor fit


def fit_floor_ransac(
    xyz: np.ndarray,
    prior_floor_z: float,
    band_m: float = 0.35,
    iters: int = 300,
    inlier_thresh_m: float = 0.03,
    rng_seed: int = 7,
):
    """RANSAC plane fit restricted to a band around the expected floor height.

    The band prior (map z of the floor ~= -LIDAR_HEIGHT_OFFSET_M) stops the
    fit from latching onto a large table or bed instead of the floor.
    Returns (unit normal pointing up, plane point).
    """
    band = xyz[np.abs(xyz[:, 2] - prior_floor_z) < band_m]
    if band.shape[0] < 500:
        band = xyz  # sparse map: fall back to fitting everything
    rng = np.random.default_rng(rng_seed)
    best_inliers, best = 0, None
    n_pts = band.shape[0]
    for _ in range(iters):
        idx = rng.choice(n_pts, size=3, replace=False)
        p0, p1, p2 = band[idx]
        normal = np.cross(p1 - p0, p2 - p0)
        norm = np.linalg.norm(normal)
        if norm < 1e-8:
            continue
        normal /= norm
        if abs(normal[2]) < 0.9:  # reject near-vertical planes (walls)
            continue
        dist = np.abs((band - p0) @ normal)
        inliers = int((dist < inlier_thresh_m).sum())
        if inliers > best_inliers:
            best_inliers, best = inliers, (normal, p0)
    if best is None:
        raise RuntimeError("floor fit failed — map too sparse or no horizontal plane in band")
    normal, p0 = best
    if normal[2] < 0:
        normal = -normal
    # refine with least squares on inliers
    dist = np.abs((band - p0) @ normal)
    inl = band[dist < inlier_thresh_m]
    centroid = inl.mean(axis=0)
    _, _, vt = np.linalg.svd(inl - centroid, full_matrices=False)
    normal = vt[2] / np.linalg.norm(vt[2])
    if normal[2] < 0:
        normal = -normal
    return normal, centroid, best_inliers


def floor_transform(normal: np.ndarray, point: np.ndarray) -> np.ndarray:
    """4x4 transform that maps the fitted floor plane onto z=0 (up = +z)."""
    z = normal
    x = np.cross(np.array([0.0, 1.0, 0.0]), z)
    if np.linalg.norm(x) < 1e-6:
        x = np.cross(np.array([1.0, 0.0, 0.0]), z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.stack([x, y, z])          # rows = new axes -> rotation map->floor
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = -R @ point            # plane point lands at z=0
    return T


def apply(T: np.ndarray, xyz: np.ndarray) -> np.ndarray:
    return xyz @ T[:3, :3].T + T[:3, 3]


# --------------------------------------------------------------- 2D slice


def slice_to_grid(xyz: np.ndarray, zmin: float, zmax: float, res: float):
    """Project the [zmin, zmax] band to a Nav2-style occupancy grid.

    Trinary convention: occupied=0 (black), free=254, unknown=205.
    Free space = cells inside the band's convex footprint that got floor
    returns but no obstacle returns; everything never observed = unknown.
    """
    band = xyz[(xyz[:, 2] >= zmin) & (xyz[:, 2] <= zmax)]
    floor = xyz[(xyz[:, 2] >= -0.05) & (xyz[:, 2] < zmin)]
    if band.shape[0] == 0:
        raise RuntimeError("no points in the slice band — check SLICE_MIN/MAX")
    allpts = np.vstack([band[:, :2], floor[:, :2]])
    mn = allpts.min(axis=0) - 0.5
    mx = allpts.max(axis=0) + 0.5
    w = int(np.ceil((mx[0] - mn[0]) / res))
    h = int(np.ceil((mx[1] - mn[1]) / res))
    grid = np.full((h, w), 205, dtype=np.uint8)  # unknown

    def to_idx(pts2d):
        c = ((pts2d[:, 0] - mn[0]) / res).astype(int).clip(0, w - 1)
        r = ((pts2d[:, 1] - mn[1]) / res).astype(int).clip(0, h - 1)
        return r, c

    fr, fc = to_idx(floor[:, :2])
    grid[fr, fc] = 254  # observed floor = free
    br, bc = to_idx(band[:, :2])
    grid[br, bc] = 0    # obstacle in G1 body band = occupied (wins over free)

    # PGM row 0 is the TOP of the image; ROS origin is bottom-left -> flip
    img = np.flipud(grid)
    origin = [float(mn[0]), float(mn[1]), 0.0]
    return img, origin, (w, h)


def write_pgm(path: str, img: np.ndarray) -> None:
    with open(path, "wb") as f:
        f.write(f"P5\n{img.shape[1]} {img.shape[0]}\n255\n".encode())
        f.write(img.tobytes())


# ------------------------------------------------------------------ main


def run_export(
    src_pcd: str,
    out_dir: str,
    slice_min: float,
    slice_max: float,
    resolution: float,
    lidar_height: float,
    waypoints: list | None = None,
) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    xyz = read_pcd(src_pcd)
    if xyz.shape[0] < 1000:
        raise RuntimeError(f"map has only {xyz.shape[0]} points — walk more first")

    normal, point, inliers = fit_floor_ransac(xyz, prior_floor_z=-lidar_height)
    T = floor_transform(normal, point)
    aligned = apply(T, xyz)

    write_pcd(os.path.join(out_dir, "map.pcd"), aligned)

    img, origin, (w, h) = slice_to_grid(aligned, slice_min, slice_max, resolution)
    write_pgm(os.path.join(out_dir, "map.pgm"), img)

    with open(os.path.join(out_dir, "map.yaml"), "w") as f:
        f.write(
            "image: map.pgm\n"
            f"resolution: {resolution}\n"
            f"origin: [{origin[0]:.4f}, {origin[1]:.4f}, 0.0]\n"
            "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\nmode: trinary\n"
        )

    wp_out = []
    for wp in waypoints or []:
        p = apply(T, np.array([[wp["x"], wp["y"], wp["z"]]]))[0]
        wp_out.append({**wp, "x": float(p[0]), "y": float(p[1]), "z": float(p[2]),
                       "frame": "map_floor_aligned"})
    with open(os.path.join(out_dir, "waypoints.json"), "w") as f:
        json.dump({"frame": "map_floor_aligned", "waypoints": wp_out}, f, indent=2)

    meta = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "go2-mapper (Unitree Go2 EDU, Livox Mid-360 via /utlidar)",
        "points_raw": int(xyz.shape[0]),
        "quality": quality.assess(
            grid_img=img,
            resolution_m=resolution,
            aligned_xyz=aligned,
            floor_inliers=inliers,
            lidar_height_m=lidar_height,
        ),
        "floor_fit": {
            "inliers": int(inliers),
            "normal_map_frame": [float(v) for v in normal],
            "transform_map_to_floor_aligned": T.tolist(),
        },
        "grid": {"width": w, "height": h, "resolution_m": resolution,
                 "origin": origin,
                 "slice_min_m": slice_min, "slice_max_m": slice_max,
                 "slice_note": "band chosen for Unitree G1 body height, not Go2"},
        "consumers": {
            "map.pcd": "G1 LiDAR localization (e.g. FAST-LIO-Localization) — z=0 is the floor",
            "map.yaml/map.pgm": "Nav2 map_server on the G1",
            "waypoints.json": "goal poses (e.g. front-door approach) in the same frame",
        },
    }
    with open(os.path.join(out_dir, "map_meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    return meta


if __name__ == "__main__":
    meta = run_export(
        src_pcd=sys.argv[1],
        out_dir=sys.argv[2],
        slice_min=float(os.environ.get("SLICE_MIN_M", "0.15")),
        slice_max=float(os.environ.get("SLICE_MAX_M", "1.50")),
        resolution=float(os.environ.get("GRID_RESOLUTION_M", "0.05")),
        lidar_height=float(os.environ.get("LIDAR_HEIGHT_OFFSET_M", "0.40")),
    )
    print(json.dumps(meta, indent=2))
