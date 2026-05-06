#!/usr/bin/env python3
"""
go2_lidar_filter.py

Subscribes to the Go2's onboard 3D LIDAR (Livox MID-360 on EDU stock,
exposed by Unitree's ROS2 driver as `/utlidar/cloud_deskewed`) and
derives a tiny polar obstacle map for the brain's safety wrapper.

Output: `std_msgs/String` JSON on `/go2/perception/free_space`. One
message per scan, throttled to `PUBLISH_HZ`. Schema (owned by go2-brain
at `src/brain/models.py:FreeSpace`):

    {
      "stamp_ns":       int,            # publish-time, monotonic-ish
      "sector_deg":     float,          # angular width of one sector
      "max_range_m":    float,          # distances ≥ this = "clear"
      "distances_m":    [float, ...],   # 360/sector_deg entries
      "bearing_origin": "centered"      # sector 0 starts at -180°
    }

Pipeline (vectorized; runs ~5 ms/scan with 100k+ points on Orin NX):

    raw PointCloud2 → height filter (drop floor / above-dog) →
    range filter (drop legs / out-of-reach) → 2D project (drop z) →
    sector bin (atan2 → idx) → per-sector min → publish JSON.

The brain doesn't need the cloud — it only needs
`min_distance_in_cone(bearing, half_angle)` to decide whether walking
forward is safe. The contract is intentionally tiny so brain ↔
watchtower stays cheap (~300 bytes / scan at 36 sectors).

Frame convention: REP-103 in base_link, +x forward, +y left, +bearing
CCW. Sector i (centered convention) covers
[-180 + i*sector_deg, -180 + (i+1)*sector_deg).

Tunables (env vars; defaults are sane for the Go2 EDU)
------------------------------------------------------
LIDAR_TOPIC                  /utlidar/cloud_deskewed   raw input topic
LIDAR_HEIGHT_MIN_M           0.05      drop floor returns below this (floor-relative)
LIDAR_HEIGHT_MAX_M           0.60      drop ceiling/dog-back above this (floor-relative)
LIDAR_HEIGHT_OFFSET_M        0.40      LIDAR mount height above floor; added
                                       to z to convert LIDAR-frame coords to
                                       floor-relative before filtering. Set to
                                       0 if your firmware publishes the cloud
                                       in base_link instead of the LIDAR frame.
LIDAR_RANGE_MIN_M            0.30      drop dog's own legs/body
LIDAR_RANGE_MAX_M            5.00      drop irrelevant far returns; also
                                       the "clear" sentinel value
LIDAR_SECTOR_DEG             10.0      → 36 sectors covering 360°
FREE_SPACE_HZ                10.0      throttle output rate (LIDAR is 10 Hz)
FREE_SPACE_TOPIC             /go2/perception/free_space   output

Schema drift: when you change fields here, mirror them in
`go2-brain/src/brain/models.py:FreeSpace.from_json` AND
`go2-sim/src/sim/extras_publisher.py` if sim publishes free_space too.
"""

import json
import os
import time

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from std_msgs.msg import String

try:
    from sensor_msgs.msg import PointCloud2
except ImportError:  # pragma: no cover — surfaced at runtime in main()
    PointCloud2 = None  # type: ignore[assignment]


# --- Tunables ---------------------------------------------------------------
LIDAR_TOPIC = os.environ.get("LIDAR_TOPIC", "/utlidar/cloud_deskewed")
# Height bounds are FLOOR-RELATIVE — see _scan_to_sectors for the offset.
HEIGHT_MIN_M = float(os.environ.get("LIDAR_HEIGHT_MIN_M", "0.05"))
HEIGHT_MAX_M = float(os.environ.get("LIDAR_HEIGHT_MAX_M", "0.60"))
# Livox MID-360 publishes in the LIDAR's frame (origin at the sensor).
# On a standing Go2 EDU the LIDAR sits ~0.40 m above the floor, so we
# add this offset to z before applying the floor-relative bounds above.
# Without this the filter rejects everything below ~0.45 m off the floor,
# leaving the brain's safety wrapper blind to chair legs, low boxes,
# and the lower half of every wall. Set to 0 if your firmware republishes
# the cloud in base_link.
LIDAR_HEIGHT_OFFSET_M = float(os.environ.get("LIDAR_HEIGHT_OFFSET_M", "0.40"))
RANGE_MIN_M = float(os.environ.get("LIDAR_RANGE_MIN_M", "0.30"))
RANGE_MAX_M = float(os.environ.get("LIDAR_RANGE_MAX_M", "5.00"))
SECTOR_DEG = float(os.environ.get("LIDAR_SECTOR_DEG", "10.0"))
PUBLISH_HZ = float(os.environ.get("FREE_SPACE_HZ", "10.0"))
TOPIC_OUT = os.environ.get("FREE_SPACE_TOPIC", "/go2/perception/free_space")
# ---------------------------------------------------------------------------


def _parse_pointcloud2(msg) -> np.ndarray:
    """Return an (N, 3) float32 array of (x, y, z) from a PointCloud2.

    Reads the structured byte buffer directly with numpy — much faster
    than `sensor_msgs_py.point_cloud2.read_points` (which yields a
    Python tuple per point). For a 100k-point Livox scan, this runs in
    ~1 ms vs ~50 ms for the iterator approach.

    Assumes the standard Livox/PointCloud2 layout: x, y, z are float32
    fields somewhere in `point_step` bytes per point. Other fields
    (intensity, ring, t, etc.) are skipped via the offset lookup."""
    if msg.width * msg.height == 0:
        return np.zeros((0, 3), dtype=np.float32)

    fields = {f.name: f for f in msg.fields}
    if not all(k in fields for k in ("x", "y", "z")):
        # If the driver speaks a non-standard cloud (no named x/y/z) we
        # can't decode without per-firmware special cases. Fail loud
        # rather than silently returning garbage.
        raise ValueError(
            f"PointCloud2 missing x/y/z fields; got {list(fields)}"
        )
    if any(fields[k].datatype != 7 for k in ("x", "y", "z")):
        # datatype 7 == FLOAT32 in sensor_msgs/PointField.
        raise ValueError(
            "PointCloud2 x/y/z must be FLOAT32; "
            f"got types {[fields[k].datatype for k in ('x','y','z')]}"
        )

    n = msg.width * msg.height
    point_step = msg.point_step
    raw = np.frombuffer(msg.data, dtype=np.uint8)
    if raw.size < n * point_step:
        # Truncated message — happens occasionally on a flaky link.
        # Process whatever full points we have rather than crashing.
        n = raw.size // point_step
        if n == 0:
            return np.zeros((0, 3), dtype=np.float32)
    rows = raw[: n * point_step].reshape(n, point_step)

    # Slice each axis out of the per-point stride and view as float32.
    out = np.empty((n, 3), dtype=np.float32)
    for i, name in enumerate(("x", "y", "z")):
        off = fields[name].offset
        out[:, i] = rows[:, off : off + 4].copy().view(np.float32).ravel()
    return out


def _scan_to_sectors(
    pts: np.ndarray, n_sectors: int, sector_deg: float, max_range_m: float,
) -> np.ndarray:
    """Vectorized: filter by height + range → bin by bearing → per-sector min.

    Returns a length-`n_sectors` float32 array. Empty sectors get
    `max_range_m` (the "clear" sentinel)."""
    if pts.size == 0:
        return np.full(n_sectors, max_range_m, dtype=np.float32)

    # LIDAR-frame z → floor-relative z. See LIDAR_HEIGHT_OFFSET_M comment
    # in the tunables block. Without this the floor-relative HEIGHT_*
    # bounds reject everything below ~0.45 m off the floor.
    z = pts[:, 2] + LIDAR_HEIGHT_OFFSET_M
    height_mask = (z > HEIGHT_MIN_M) & (z < HEIGHT_MAX_M)
    pts = pts[height_mask]
    if pts.size == 0:
        return np.full(n_sectors, max_range_m, dtype=np.float32)

    x, y = pts[:, 0], pts[:, 1]
    rng = np.hypot(x, y)
    range_mask = (rng > RANGE_MIN_M) & (rng < max_range_m)
    x, y, rng = x[range_mask], y[range_mask], rng[range_mask]
    if x.size == 0:
        return np.full(n_sectors, max_range_m, dtype=np.float32)

    # atan2 returns [-π, π] → degrees [-180, 180]. Bin edge: idx i covers
    # [-180 + i*sector_deg, -180 + (i+1)*sector_deg).
    bearing_deg = np.degrees(np.arctan2(y, x))
    idx = ((bearing_deg + 180.0) / sector_deg).astype(np.int32)
    np.clip(idx, 0, n_sectors - 1, out=idx)

    # Per-sector min via np.minimum.at (handles duplicate bin indices —
    # a plain `out[idx] = rng` would clobber instead of taking the min).
    sector_min = np.full(n_sectors, max_range_m, dtype=np.float32)
    np.minimum.at(sector_min, idx, rng)
    return sector_min


class Go2LidarFilter(Node):
    def __init__(self):
        super().__init__("go2_lidar_filter")

        self._n_sectors = int(round(360.0 / SECTOR_DEG))
        if abs(self._n_sectors * SECTOR_DEG - 360.0) > 1e-6:
            self.get_logger().warning(
                f"sector_deg={SECTOR_DEG} doesn't divide 360 evenly; "
                f"using {self._n_sectors} sectors"
            )

        self.pub = self.create_publisher(String, TOPIC_OUT, 10)

        # LIDAR clouds use BEST_EFFORT/SENSOR_DATA QoS — match it or the
        # subscription gets nothing on most Unitree firmwares.
        qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=4,
        )
        self.create_subscription(PointCloud2, LIDAR_TOPIC, self._on_cloud, qos)

        self._latest_sectors: np.ndarray | None = None
        self._period_s = 1.0 / max(PUBLISH_HZ, 0.1)
        self.create_timer(self._period_s, self._publish)

        # Throttled "no scans yet" warning so the log isn't spammed when
        # the LIDAR topic name is wrong / driver isn't running.
        self._last_warn = 0.0
        self._got_first_scan = False

        self.get_logger().info(
            f"lidar filter: {LIDAR_TOPIC} → {TOPIC_OUT} "
            f"@ {PUBLISH_HZ:.1f} Hz, {self._n_sectors} sectors x "
            f"{SECTOR_DEG:.1f}°, range [{RANGE_MIN_M:.2f}, {RANGE_MAX_M:.2f}] m, "
            f"height [{HEIGHT_MIN_M:.2f}, {HEIGHT_MAX_M:.2f}] m"
        )

    def _on_cloud(self, msg) -> None:
        try:
            pts = _parse_pointcloud2(msg)
        except ValueError as exc:
            # Schema drift / unexpected cloud format. Don't kill the node;
            # log once per second so it's visible without spamming.
            now = time.monotonic()
            if now - self._last_warn > 1.0:
                self.get_logger().warning(f"pointcloud parse failed: {exc}")
                self._last_warn = now
            return
        sectors = _scan_to_sectors(
            pts, self._n_sectors, SECTOR_DEG, RANGE_MAX_M,
        )
        self._latest_sectors = sectors
        if not self._got_first_scan:
            self._got_first_scan = True
            # z-range diagnostic helps verify LIDAR_HEIGHT_OFFSET_M.
            # In LIDAR frame, floor returns sit near z = -mount_height
            # (~-0.40 m on Go2 EDU). If you see z biased ~+0.4 m, the
            # cloud is already in base_link → set LIDAR_HEIGHT_OFFSET_M=0.
            z_raw = pts[:, 2] if pts.size else np.zeros(1)
            self.get_logger().info(
                f"first scan: {pts.shape[0]} raw points, "
                f"{int(np.sum(sectors < RANGE_MAX_M))} sectors with returns, "
                f"z_lidar range [{float(z_raw.min()):.2f}, "
                f"{float(z_raw.max()):.2f}] m (offset={LIDAR_HEIGHT_OFFSET_M:.2f} → "
                f"floor-relative slab [{HEIGHT_MIN_M:.2f}, {HEIGHT_MAX_M:.2f}] m)"
            )

    def _publish(self) -> None:
        sectors = self._latest_sectors
        if sectors is None:
            return
        payload = {
            "stamp_ns":       time.time_ns(),
            "sector_deg":     SECTOR_DEG,
            "max_range_m":    RANGE_MAX_M,
            "distances_m":    [round(float(d), 3) for d in sectors.tolist()],
            "bearing_origin": "centered",
        }
        msg = String()
        msg.data = json.dumps(payload, separators=(",", ":"))
        self.pub.publish(msg)


def main():
    rclpy.init()
    if PointCloud2 is None:
        print(
            "go2_lidar_filter: sensor_msgs not importable — free_space "
            "publisher disabled. The brain's safety wrapper will fall "
            "back to pass-through (or fail-safe in BRAIN_SAFETY_STRICT=1)."
        )
        rclpy.try_shutdown()
        raise SystemExit(1)
    try:
        node = Go2LidarFilter()
    except Exception as exc:
        print(f"go2_lidar_filter: init failed: {exc}")
        rclpy.try_shutdown()
        raise
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
