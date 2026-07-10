#!/usr/bin/env python3
"""
refine.py — offline loop-closure refinement of a drifted Point-LIO map.
[robustness #2]

Point-LIO is pure odometry: no loop closure, so walking a full home and
returning to the start leaves the map with accumulated drift — the classic
"doubled walls" that quality.py flags but the raw export can't fix.

This stage fixes it *offline*, from the keyframes go2-slam's viz_bridge drops
every KEYFRAME_DIST_M (pose + a downsampled scan). We:

  1. Build a planar SE(2) pose graph — one node per keyframe. A home map's
     drift is dominated by in-plane (x, y, yaw) error, and the export already
     assumes a single floor, so SE(2) is the right, tractable model.
  2. Odometry edges lock consecutive keyframes to their measured relative pose.
  3. Loop edges: for keyframe pairs that are spatially close but temporally far
     apart (you walked back to a place), 2D-ICP their scans to measure the true
     relative pose, weighted by ICP fitness.
  4. Gauss-Newton optimise the graph (node 0 fixed) to distribute the drift.
  5. Re-stitch: apply each keyframe's pose correction to its scan and merge.

Output: map_refined.pcd in the same map frame as map.pcd, so the existing
floor-fit + slice export consumes it unchanged.

EXPERIMENTAL: the graph math is standard (Grisetti et al. SE(2) PGO) but this
has not been validated against ground truth on a real Go2 walk. It only
replaces the raw map when it finds confident loops and the optimisation
reduces loop error; otherwise it declines and you keep the raw map. Always
eyeball map_refined.pcd in Foxglove before trusting it on the G1.
"""

import glob
import json
import math
import os

import numpy as np
from scipy.spatial import cKDTree

# ---- tunables (env, baked in the console Dockerfile) --------------------
LOOP_RADIUS_M = float(os.environ.get("LOOP_RADIUS_M", "2.0"))       # candidate proximity
LOOP_MIN_GAP = int(os.environ.get("LOOP_MIN_GAP", "25"))            # keyframes apart
ICP_MAX_ITERS = int(os.environ.get("ICP_MAX_ITERS", "30"))
ICP_INLIER_M = float(os.environ.get("ICP_INLIER_M", "0.30"))       # correspondence gate
ICP_MIN_FITNESS = float(os.environ.get("ICP_MIN_FITNESS", "0.45"))  # accept a loop
MAX_LOOPS_PER_KF = int(os.environ.get("MAX_LOOPS_PER_KF", "3"))     # curb aliasing blow-up
PGO_ITERS = int(os.environ.get("PGO_ITERS", "20"))
EXPORT_VOXEL = float(os.environ.get("EXPORT_VOXEL_M", "0.03"))      # output resolution

_PACK_OFF = 1 << 20  # voxel-index offset so signed map-frame coords pack cleanly


# ------------------------------------------------------------ SE(2) utils
def v2t(x, y, th):
    c, s = math.cos(th), math.sin(th)
    return np.array([[c, -s, x], [s, c, y], [0, 0, 1.0]])


def t2v(T):
    return np.array([T[0, 2], T[1, 2], math.atan2(T[1, 0], T[0, 0])])


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def yaw_from_quat(qx, qy, qz, qw):
    return math.atan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))


# --------------------------------------------------------------- keyframes
def load_keyframes(kf_dir):
    """Return list of dicts: {seq, x, y, z, yaw, xyz(world Nx3)}."""
    kfs = []
    for path in sorted(glob.glob(os.path.join(kf_dir, "kf_*.npz"))):
        d = np.load(path)
        pose = d["pose"]  # x y z qx qy qz qw
        kfs.append({
            "seq": int(d["seq"]),
            "x": float(pose[0]), "y": float(pose[1]), "z": float(pose[2]),
            "yaw": yaw_from_quat(*pose[3:7]),
            "xyz": d["xyz"].astype(np.float64),
        })
    return kfs


# ---------------------------------------------------------------- 2D ICP
def icp_2d(src, dst, init=np.eye(3), max_iters=ICP_MAX_ITERS, inlier=ICP_INLIER_M):
    """Point-to-point 2D ICP. Returns (T 3x3, fitness, rmse).

    fitness = fraction of src points with a dst correspondence within `inlier`.
    """
    if src.shape[0] < 30 or dst.shape[0] < 30:
        return init, 0.0, float("inf")
    tree = cKDTree(dst)
    T = init.copy()
    fitness, rmse = 0.0, float("inf")
    for _ in range(max_iters):
        p = (src @ T[:2, :2].T) + T[:2, 2]
        dists, idx = tree.query(p, k=1)
        m = dists < inlier
        if m.sum() < 10:
            break
        a = p[m]
        b = dst[idx[m]]
        ca, cb = a.mean(0), b.mean(0)
        H = (a - ca).T @ (b - cb)
        U, _, Vt = np.linalg.svd(H)
        R = Vt.T @ U.T
        if np.linalg.det(R) < 0:               # reflection guard
            Vt[-1] *= -1
            R = Vt.T @ U.T
        t = cb - R @ ca
        step = np.eye(3)
        step[:2, :2] = R
        step[:2, 2] = t
        T = step @ T
        fitness = float(m.mean())
        rmse = float(np.sqrt((dists[m] ** 2).mean()))
        if np.hypot(t[0], t[1]) < 1e-4 and abs(math.atan2(R[1, 0], R[0, 0])) < 1e-4:
            break
    return T, fitness, rmse


# ------------------------------------------------------ pose-graph solve
def optimise(nodes, edges, iters=PGO_ITERS):
    """Gauss-Newton on SE(2) pose graph. nodes: (N,3) x,y,theta. edges:
    list of (i, j, z(3,), omega(3x3)). Node 0 anchored. Returns optimised nodes."""
    x = nodes.copy()
    N = x.shape[0]
    for _ in range(iters):
        H = np.zeros((3 * N, 3 * N))
        b = np.zeros(3 * N)
        for i, j, z, om in edges:
            Ti, Tj, Z = v2t(*x[i]), v2t(*x[j]), v2t(*z)
            e = t2v(np.linalg.inv(Z) @ np.linalg.inv(Ti) @ Tj)
            e[2] = wrap(e[2])
            th_i = x[i, 2]
            si, ci = math.sin(th_i), math.cos(th_i)
            Rz = Z[:2, :2]
            RiT = np.array([[ci, si], [-si, ci]])
            dRiT = np.array([[-si, ci], [-ci, -si]])   # d(Ri^T)/dtheta_i
            dt = (x[j, :2] - x[i, :2])
            A = np.zeros((3, 3))
            A[:2, :2] = -Rz.T @ RiT
            A[:2, 2] = Rz.T @ dRiT @ dt
            A[2, 2] = -1.0
            B = np.zeros((3, 3))
            B[:2, :2] = Rz.T @ RiT
            B[2, 2] = 1.0
            ii, jj = slice(3 * i, 3 * i + 3), slice(3 * j, 3 * j + 3)
            H[ii, ii] += A.T @ om @ A
            H[ii, jj] += A.T @ om @ B
            H[jj, ii] += B.T @ om @ A
            H[jj, jj] += B.T @ om @ B
            b[ii] += A.T @ om @ e
            b[jj] += B.T @ om @ e
        H[:3, :3] += np.eye(3) * 1e6          # anchor node 0
        try:
            dx = np.linalg.solve(H, -b)
        except np.linalg.LinAlgError:
            break
        x += dx.reshape(N, 3)
        x[:, 2] = (x[:, 2] + math.pi) % (2 * math.pi) - math.pi
        if np.linalg.norm(dx) < 1e-4:
            break
    return x


# ----------------------------------------------------------------- driver
def run_refine(kf_dir, out_pcd):
    """Refine and write out_pcd. Returns a summary dict."""
    kfs = load_keyframes(kf_dir)
    if len(kfs) < LOOP_MIN_GAP + 2:
        return {"ok": False, "reason": f"only {len(kfs)} keyframes — walk more "
                "(and loop back) before refining", "keyframes": len(kfs)}

    nodes = np.array([[k["x"], k["y"], k["yaw"]] for k in kfs])
    xy = nodes[:, :2]

    edges = []
    odom_om = np.diag([1.0 / 0.05**2, 1.0 / 0.05**2, 1.0 / 0.02**2])  # tight
    for i in range(len(kfs) - 1):
        z = t2v(np.linalg.inv(v2t(*nodes[i])) @ v2t(*nodes[i + 1]))
        edges.append((i, i + 1, z, odom_om))

    # loop candidates: close in space, far in sequence. Collect per source
    # keyframe so we can keep only the best-fitness few — long identical
    # corridors alias badly and a flood of weak matches warps the graph.
    tree = cKDTree(xy)
    cand = {}  # i -> list of (fitness, j, z)
    for i, j in sorted(tree.query_pairs(LOOP_RADIUS_M)):
        if abs(i - j) < LOOP_MIN_GAP:
            continue
        a = kfs[i]["xyz"][:, :2]  # walls dominate the XY projection -> good 2D ICP
        b = kfs[j]["xyz"][:, :2]
        init = np.eye(3)
        init[:2, 2] = b.mean(0) - a.mean(0)   # centroid head-start widens the ICP basin
        T_align, fitness, _ = icp_2d(a, b, init=init)  # T_align: T_align @ P_i ~= P_j
        if fitness < ICP_MIN_FITNESS:
            continue
        # relative pose i->j the scan match implies (derivation in module docstring):
        # Z_ij = X_i^-1 . T_align^-1 . X_j
        z = t2v(np.linalg.inv(v2t(*nodes[i])) @ np.linalg.inv(T_align) @ v2t(*nodes[j]))
        cand.setdefault(i, []).append((fitness, j, z))

    loops = 0
    for i, lst in cand.items():
        for fitness, j, z in sorted(lst, key=lambda e: -e[0])[:MAX_LOOPS_PER_KF]:
            w = fitness  # weight loop edges by ICP confidence
            edges.append((i, j, z, np.diag([w / 0.10**2, w / 0.10**2, w / 0.05**2])))
            loops += 1

    if loops == 0:
        return {"ok": False, "reason": "no confident loop closures found — walk a "
                "closed loop (return to a previously-mapped spot)", "keyframes": len(kfs)}

    def loop_err(ns):
        tot = 0.0
        for i, j, z, om in edges[len(kfs) - 1:]:  # loop edges only
            e = t2v(np.linalg.inv(v2t(*z)) @ np.linalg.inv(v2t(*ns[i])) @ v2t(*ns[j]))
            e[2] = wrap(e[2])
            tot += float(e @ om @ e)
        return tot

    before = loop_err(nodes)
    opt = optimise(nodes, edges)
    after = loop_err(opt)
    if not math.isfinite(after) or after > before:
        return {"ok": False, "reason": "optimisation did not reduce loop error — "
                "keeping raw map", "keyframes": len(kfs), "loops": loops,
                "loop_err_before": round(before, 3), "loop_err_after": round(after, 3)}

    # re-stitch: apply each keyframe's SE(2) correction to its world scan
    chunks = []
    for k, orig, corr in zip(kfs, nodes, opt):
        D = v2t(*corr) @ np.linalg.inv(v2t(*orig))   # world-frame correction
        p = k["xyz"]
        xy2 = (p[:, :2] @ D[:2, :2].T) + D[:2, 2]
        chunks.append(np.column_stack([xy2, p[:, 2]]))
    merged = np.vstack(chunks)

    # voxel-downsample to export resolution (offset so negative coords pack cleanly)
    ijk = (np.floor(merged / EXPORT_VOXEL).astype(np.int64) + _PACK_OFF)
    key = (ijk[:, 0] << 42) | (ijk[:, 1] << 21) | ijk[:, 2]
    _, idx = np.unique(key, return_index=True)
    merged = merged[idx].astype(np.float32)

    _write_pcd(out_pcd, merged)
    return {"ok": True, "keyframes": len(kfs), "loops": loops,
            "loop_err_before": round(before, 3), "loop_err_after": round(after, 3),
            "points": int(merged.shape[0]), "output": os.path.basename(out_pcd)}


def _write_pcd(path, xyz):
    header = (
        "# .PCD v0.7 - Point Cloud Data file format\n"
        "VERSION 0.7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\n"
        f"WIDTH {xyz.shape[0]}\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\n"
        f"POINTS {xyz.shape[0]}\nDATA binary\n"
    )
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(header.encode())
        f.write(xyz.astype("<f4").tobytes())
    os.replace(tmp, path)


if __name__ == "__main__":
    import sys
    print(json.dumps(run_refine(sys.argv[1], sys.argv[2]), indent=2))
