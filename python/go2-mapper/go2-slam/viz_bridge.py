#!/usr/bin/env python3
"""
viz_bridge.py — sidecar for go2-slam. Jobs:

1. VIZ MAP    Subscribe to Point-LIO's registered scan output, accumulate a
              voxel map, republish a coarse (VIZ_VOXEL_M / VIZ_HZ) copy on
              /go2/slam/map_viz. Full-rate accumulating clouds choke Foxglove
              over Wi-Fi; a downsampled copy stays smooth on a phone.

2. POSE JSON  Republish Point-LIO odometry (/aft_mapped_to_init) as a tiny
              std_msgs/String JSON on /go2/slam/pose_json so the console can
              read it via bare CycloneDDS without an rclpy install — the same
              trick go2-foxglove uses for its perception inputs.

3. SNAPSHOT   Every MAP_SAVE_INTERVAL_S, dump the accumulated map to
              $MAP_SAVE_DIR/current/map.pcd (atomic rename), plus a rolling
              ring of the last SNAPSHOT_KEEP snapshots under current/snapshots/.
              The console's "Finish & export" reads the latest snapshot, so a
              crash never costs you the whole walk.

              The exported map is voxelised at EXPORT_VOXEL_M (finer than the
              Foxglove copy) — the 3D cloud is a G1 localisation prior, so it
              wants more detail than the phone preview. [robustness #1]

4. DYNAMICS   Each voxel tracks how many frames observed it and over what time
              span. The snapshot culls voxels seen in fewer than MIN_VOXEL_HITS
              frames (single-frame noise / fast-moving ghosts); raising
              DYNAMIC_MIN_SPAN_S additionally drops voxels only ever seen within
              a short window (people/pets that walked through). [robustness #4]

5. HEALTH     A watchdog on the odometry flags SLAM divergence (NaN pose,
              teleport-sized jumps, out-of-range coordinates) on
              /go2/slam/health_json. While diverged, snapshots are frozen so a
              blown-up map never overwrites the last good one. [robustness #3]

6. KEYFRAMES  Every KEYFRAME_DIST_M of travel, save (pose + a downsampled scan)
              under current/keyframes/. The console's offline loop-closure
              refine (refine.py) consumes these to correct odometry drift.
              [robustness #2]

Frames: everything stays in Point-LIO's map frame (gravity-aligned, origin
at SLAM start). Floor alignment to z=0 happens once, in the console export.
"""

import json
import math
import os
import signal
import threading
import time

import numpy as np
import rclpy
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import QoSPresetProfiles
from sensor_msgs.msg import PointCloud2, PointField
from std_msgs.msg import String

SCAN_TOPIC = os.environ.get("SCAN_TOPIC", "/cloud_registered")
ODOM_TOPIC = os.environ.get("ODOM_TOPIC", "/aft_mapped_to_init")
VIZ_TOPIC = "/go2/slam/map_viz"
POSE_TOPIC = "/go2/slam/pose_json"
HEALTH_TOPIC = "/go2/slam/health_json"

VIZ_VOXEL = float(os.environ.get("VIZ_VOXEL_M", "0.05"))
VIZ_HZ = float(os.environ.get("VIZ_HZ", "1.0"))
EXPORT_VOXEL = float(os.environ.get("EXPORT_VOXEL_M", "0.03"))  # #1 finer than viz
SAVE_DIR = os.environ.get("MAP_SAVE_DIR", "/data/maps")
SAVE_INTERVAL = float(os.environ.get("MAP_SAVE_INTERVAL_S", "30"))
SNAPSHOT_KEEP = int(os.environ.get("SNAPSHOT_KEEP", "5"))       # #3 rolling ring

MIN_VOXEL_HITS = int(os.environ.get("MIN_VOXEL_HITS", "2"))     # #4 cull transients
DYNAMIC_MIN_SPAN_S = float(os.environ.get("DYNAMIC_MIN_SPAN_S", "0.0"))  # #4 (opt-in)

# #3 watchdog thresholds
DIV_MAX_STEP_M = float(os.environ.get("DIVERGENCE_MAX_STEP_M", "1.0"))
DIV_MAX_RANGE_M = float(os.environ.get("DIVERGENCE_MAX_RANGE_M", "500.0"))

# #2 keyframes
KEYFRAME_DIST_M = float(os.environ.get("KEYFRAME_DIST_M", "0.5"))
KEYFRAME_VOXEL = float(os.environ.get("KEYFRAME_VOXEL_M", "0.08"))
KEYFRAME_DIR = os.environ.get("KEYFRAME_DIR", os.path.join(SAVE_DIR, "current", "keyframes"))

CURRENT = os.path.join(SAVE_DIR, "current")


def cloud_to_xyz(msg: PointCloud2) -> np.ndarray:
    """Extract Nx3 float32 xyz from a PointCloud2 (assumes x,y,z leading floats)."""
    step = msg.point_step
    n = len(msg.data) // step
    if n == 0:
        return np.empty((0, 3), dtype=np.float32)
    raw = np.frombuffer(bytes(msg.data), dtype=np.uint8).reshape(n, step)
    xyz = raw[:, 0:12].copy().view(np.float32).reshape(n, 3)
    return xyz[np.isfinite(xyz).all(axis=1)]


def xyz_to_cloud(xyz: np.ndarray, frame_id: str, stamp) -> PointCloud2:
    msg = PointCloud2()
    msg.header.frame_id = frame_id
    msg.header.stamp = stamp
    msg.height = 1
    msg.width = xyz.shape[0]
    msg.fields = [
        PointField(name=n, offset=4 * i, datatype=PointField.FLOAT32, count=1)
        for i, n in enumerate(("x", "y", "z"))
    ]
    msg.is_bigendian = False
    msg.point_step = 12
    msg.row_step = 12 * xyz.shape[0]
    msg.is_dense = True
    msg.data = xyz.astype(np.float32).tobytes()
    return msg


_PACK_OFF = 1 << 20  # voxel-index offset so negatives pack into unsigned fields


def _pack_keys(ijk: np.ndarray) -> np.ndarray:
    """Pack Nx3 int voxel indices into N int64 keys (21 bits/axis, signed)."""
    a = (ijk[:, 0].astype(np.int64) + _PACK_OFF) << 42
    b = (ijk[:, 1].astype(np.int64) + _PACK_OFF) << 21
    c = ijk[:, 2].astype(np.int64) + _PACK_OFF
    return a | b | c


def voxel_downsample(xyz: np.ndarray, voxel: float) -> np.ndarray:
    """Keep one representative point per `voxel`-sized cell. Vectorised."""
    if xyz.shape[0] == 0:
        return xyz
    ijk = np.floor(xyz / voxel).astype(np.int64)
    _, idx = np.unique(_pack_keys(ijk), return_index=True)
    return xyz[idx]


class VoxelGrid:
    """Vectorised voxel map with per-voxel observation stats.

    First-seen point wins per cell. Amortised-O(1) growth via a doubling
    backing store; the only Python-level loop is over *new* unique cells in a
    single scan (a few thousand at most), not over every point. [#1]

    Per cell we keep an observation count and first/last observation time so
    the snapshot can drop transient returns. [#4]
    """

    def __init__(self, voxel: float):
        self.voxel = float(voxel)
        self._row = {}                                  # packed key -> row index
        self._cap = 0
        self._n = 0
        self._pts = np.empty((0, 3), dtype=np.float32)
        self._hits = np.empty((0,), dtype=np.int32)
        self._first_t = np.empty((0,), dtype=np.float64)
        self._last_t = np.empty((0,), dtype=np.float64)
        self._lock = threading.Lock()

    def _grow(self, extra: int) -> None:
        need = self._n + extra
        if need <= self._cap:
            return
        newcap = max(need, self._cap * 2, 1 << 16)
        self._pts = np.resize(self._pts, (newcap, 3))
        self._hits = np.resize(self._hits, newcap)
        self._first_t = np.resize(self._first_t, newcap)
        self._last_t = np.resize(self._last_t, newcap)
        self._cap = newcap

    def insert(self, xyz: np.ndarray, t: float) -> None:
        if xyz.shape[0] == 0:
            return
        ijk = np.floor(xyz / self.voxel).astype(np.int64)
        keys = _pack_keys(ijk)
        uk, first_idx = np.unique(keys, return_index=True)  # unique cells this scan
        with self._lock:
            rows = np.fromiter((self._row.get(int(k), -1) for k in uk),
                               dtype=np.int64, count=uk.shape[0])
            seen = rows >= 0
            if seen.any():
                er = rows[seen]
                self._hits[er] += 1
                self._last_t[er] = t
            new = ~seen
            if new.any():
                nk = uk[new]
                npts = xyz[first_idx[new]].astype(np.float32)
                base = self._n
                self._grow(nk.shape[0])
                sl = slice(base, base + nk.shape[0])
                self._pts[sl] = npts
                self._hits[sl] = 1
                self._first_t[sl] = t
                self._last_t[sl] = t
                for j, k in enumerate(nk.tolist()):
                    self._row[k] = base + j
                self._n = base + nk.shape[0]

    def snapshot(self, min_hits: int = 1, min_span_s: float = 0.0) -> np.ndarray:
        """Cull transient cells (#4), return the surviving points."""
        with self._lock:
            n = self._n
            if n == 0:
                return np.empty((0, 3), dtype=np.float32)
            keep = self._hits[:n] >= min_hits
            if min_span_s > 0.0:
                span = self._last_t[:n] - self._first_t[:n]
                keep &= span >= min_span_s
            return self._pts[:n][keep].copy()


def write_pcd(path: str, xyz: np.ndarray) -> None:
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
    os.replace(tmp, path)  # atomic — console never reads a half-written map


class VizBridge(Node):
    def __init__(self):
        super().__init__("go2_viz_bridge")
        self.grid = VoxelGrid(EXPORT_VOXEL)
        self.latest_pose = None
        self.last_scan = np.empty((0, 3), dtype=np.float32)  # for keyframe capture
        self._snap_seq = 0
        self._kf_seq = 0
        self._kf_last_xy = None
        self._prev_odom = None                                # (x, y, t) for watchdog
        self.health = {"status": "ok", "reason": None, "since_s": 0.0}
        self._t0 = time.monotonic()

        sensor_qos = QoSPresetProfiles.SENSOR_DATA.value
        self.create_subscription(PointCloud2, SCAN_TOPIC, self.on_scan, sensor_qos)
        self.create_subscription(Odometry, ODOM_TOPIC, self.on_odom, 20)
        self.viz_pub = self.create_publisher(PointCloud2, VIZ_TOPIC, 1)
        self.pose_pub = self.create_publisher(String, POSE_TOPIC, 10)
        self.health_pub = self.create_publisher(String, HEALTH_TOPIC, 10)
        self.create_timer(1.0 / VIZ_HZ, self.publish_viz)
        self.create_timer(SAVE_INTERVAL, self.save_snapshot)
        os.makedirs(CURRENT, exist_ok=True)
        os.makedirs(os.path.join(CURRENT, "snapshots"), exist_ok=True)
        os.makedirs(KEYFRAME_DIR, exist_ok=True)
        self.get_logger().info(
            f"export@{EXPORT_VOXEL}m viz@{VIZ_VOXEL}m/{VIZ_HZ}Hz  "
            f"snapshots every {SAVE_INTERVAL}s (keep {SNAPSHOT_KEEP})  "
            f"min_hits={MIN_VOXEL_HITS} keyframes@{KEYFRAME_DIST_M}m -> {CURRENT}"
        )

    # ---------------------------------------------------------- callbacks
    def on_scan(self, msg: PointCloud2):
        xyz = cloud_to_xyz(msg)
        self.last_scan = xyz
        self.grid.insert(xyz, time.monotonic() - self._t0)

    def on_odom(self, msg: Odometry):
        p, q = msg.pose.pose.position, msg.pose.pose.orientation
        now = time.monotonic()
        self._check_health(p, now)
        self.latest_pose = {
            "stamp_ns": time.monotonic_ns(),
            "frame": "map",
            "x": p.x, "y": p.y, "z": p.z,
            "qx": q.x, "qy": q.y, "qz": q.z, "qw": q.w,
        }
        out = String()
        out.data = json.dumps(self.latest_pose)
        self.pose_pub.publish(out)
        self._maybe_keyframe(p)

    # ------------------------------------------------------ health (#3)
    def _check_health(self, p, now: float):
        reason = None
        if not all(math.isfinite(v) for v in (p.x, p.y, p.z)):
            reason = "non-finite pose (Point-LIO diverged)"
        elif max(abs(p.x), abs(p.y), abs(p.z)) > DIV_MAX_RANGE_M:
            reason = f"pose out of range (>{DIV_MAX_RANGE_M:.0f} m)"
        elif self._prev_odom is not None:
            px, py, pt = self._prev_odom
            dt = max(now - pt, 1e-3)
            step = math.hypot(p.x - px, p.y - py)
            if step > DIV_MAX_STEP_M and step / dt > DIV_MAX_STEP_M / 0.1:
                reason = f"pose jump {step:.2f} m in {dt*1e3:.0f} ms"
        if math.isfinite(p.x) and math.isfinite(p.y):
            self._prev_odom = (p.x, p.y, now)

        if reason:
            if self.health["status"] == "ok":
                self.health = {"status": "diverged", "reason": reason,
                               "since_s": now - self._t0}
                self.get_logger().error(f"SLAM health: DIVERGED — {reason}; "
                                        "freezing map snapshots")
        # once diverged we stay diverged until the node/SLAM is restarted:
        # a diverged Point-LIO doesn't self-recover, and silently resuming
        # snapshots would let a broken map overwrite the last good one.
        h = String()
        h.data = json.dumps(self.health)
        self.health_pub.publish(h)

    # ------------------------------------------------- keyframes (#2)
    def _maybe_keyframe(self, p):
        if self.health["status"] != "ok" or self.last_scan.shape[0] == 0:
            return
        xy = (p.x, p.y)
        if (self._kf_last_xy is not None
                and math.hypot(xy[0] - self._kf_last_xy[0],
                               xy[1] - self._kf_last_xy[1]) < KEYFRAME_DIST_M):
            return
        self._kf_last_xy = xy
        pose = self.latest_pose
        cloud = voxel_downsample(self.last_scan, KEYFRAME_VOXEL)
        path = os.path.join(KEYFRAME_DIR, f"kf_{self._kf_seq:05d}.npz")
        tmp = os.path.join(KEYFRAME_DIR, f".kf_{self._kf_seq:05d}.tmp.npz")  # .npz so np won't re-suffix
        np.savez_compressed(
            tmp,
            xyz=cloud.astype(np.float32),
            pose=np.array([pose["x"], pose["y"], pose["z"],
                           pose["qx"], pose["qy"], pose["qz"], pose["qw"]],
                          dtype=np.float64),
            seq=self._kf_seq,
        )
        os.replace(tmp, path)  # atomic publish of the keyframe
        self._kf_seq += 1

    # ----------------------------------------------------------- outputs
    def publish_viz(self):
        pts = self.grid.snapshot(min_hits=1)  # preview: show everything, coarse
        if pts.shape[0]:
            self.viz_pub.publish(
                xyz_to_cloud(voxel_downsample(pts, VIZ_VOXEL),
                             "camera_init", self.get_clock().now().to_msg())
            )

    def save_snapshot(self, force: bool = False):
        if self.health["status"] != "ok" and not force:
            return  # #3: don't overwrite the last good map with a diverged one
        pts = self.grid.snapshot(min_hits=MIN_VOXEL_HITS, min_span_s=DYNAMIC_MIN_SPAN_S)
        if pts.shape[0] == 0:
            return
        ring = os.path.join(CURRENT, "snapshots", f"map_{self._snap_seq:04d}.pcd")
        write_pcd(ring, pts)
        write_pcd(os.path.join(CURRENT, "map.pcd"), pts)  # latest, atomic
        self._snap_seq += 1
        self._prune_snapshots()

    def _prune_snapshots(self):
        d = os.path.join(CURRENT, "snapshots")
        snaps = sorted(f for f in os.listdir(d) if f.endswith(".pcd"))
        for f in snaps[:-SNAPSHOT_KEEP] if SNAPSHOT_KEEP > 0 else []:
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass


def main():
    rclpy.init()
    node = VizBridge()

    def _shutdown(*_):
        node.get_logger().info("shutting down — flushing final snapshot")
        node.save_snapshot(force=True)
        if rclpy.ok():
            rclpy.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)  # container stop
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.save_snapshot(force=True)


if __name__ == "__main__":
    main()
