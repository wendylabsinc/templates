#!/usr/bin/env python3
"""
viz_bridge.py — sidecar for go2-slam. Three jobs:

1. VIZ MAP   Subscribe to Point-LIO's registered scan output, accumulate a
             voxel-downsampled cloud, republish at VIZ_HZ on /go2/slam/map_viz.
             Full-rate accumulating clouds choke Foxglove over Wi-Fi; a 5 cm /
             1 Hz copy stays smooth on a phone while SLAM keeps full res.

2. POSE JSON Republish Point-LIO odometry (/aft_mapped_to_init) as a tiny
             std_msgs/String JSON on /go2/slam/pose_json so the console can
             read it via bare CycloneDDS without an rclpy install — the same
             trick go2-foxglove uses for its perception inputs.

3. SNAPSHOT  Every MAP_SAVE_INTERVAL_S, dump the accumulated full-res cloud
             to $MAP_SAVE_DIR/current/map.pcd (atomic rename). The console's
             "Finish & export" reads the latest snapshot, so a crash never
             costs you the whole walk.

Frames: everything stays in Point-LIO's map frame (gravity-aligned, origin
at SLAM start). Floor alignment to z=0 happens once, in the console export.
"""

import json
import os
import struct
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
VIZ_VOXEL = float(os.environ.get("VIZ_VOXEL_M", "0.05"))
VIZ_HZ = float(os.environ.get("VIZ_HZ", "1.0"))
SAVE_DIR = os.environ.get("MAP_SAVE_DIR", "/data/maps")
SAVE_INTERVAL = float(os.environ.get("MAP_SAVE_INTERVAL_S", "30"))


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


class VoxelAccumulator:
    """Dict-of-voxels map: O(points) insert, bounded memory, thread-safe."""

    def __init__(self, voxel: float):
        self.voxel = voxel
        self._centers: dict[tuple, np.ndarray] = {}
        self._lock = threading.Lock()

    def insert(self, xyz: np.ndarray) -> None:
        keys = np.floor(xyz / self.voxel).astype(np.int32)
        with self._lock:
            for k, p in zip(map(tuple, keys), xyz):
                if k not in self._centers:
                    self._centers[k] = p

    def snapshot(self) -> np.ndarray:
        with self._lock:
            if not self._centers:
                return np.empty((0, 3), dtype=np.float32)
            return np.stack(list(self._centers.values()))


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
        self.acc = VoxelAccumulator(VIZ_VOXEL)
        self.latest_pose = None
        sensor_qos = QoSPresetProfiles.SENSOR_DATA.value
        self.create_subscription(PointCloud2, SCAN_TOPIC, self.on_scan, sensor_qos)
        self.create_subscription(Odometry, ODOM_TOPIC, self.on_odom, 20)
        self.viz_pub = self.create_publisher(PointCloud2, VIZ_TOPIC, 1)
        self.pose_pub = self.create_publisher(String, POSE_TOPIC, 10)
        self.create_timer(1.0 / VIZ_HZ, self.publish_viz)
        self.create_timer(SAVE_INTERVAL, self.save_snapshot)
        os.makedirs(os.path.join(SAVE_DIR, "current"), exist_ok=True)
        self.get_logger().info(
            f"viz={VIZ_VOXEL}m@{VIZ_HZ}Hz  snapshots every {SAVE_INTERVAL}s -> {SAVE_DIR}/current/map.pcd"
        )

    def on_scan(self, msg: PointCloud2):
        self.acc.insert(cloud_to_xyz(msg))

    def on_odom(self, msg: Odometry):
        p, q = msg.pose.pose.position, msg.pose.pose.orientation
        self.latest_pose = {
            "stamp_ns": time.monotonic_ns(),
            "frame": "map",
            "x": p.x, "y": p.y, "z": p.z,
            "qx": q.x, "qy": q.y, "qz": q.z, "qw": q.w,
        }
        out = String()
        out.data = json.dumps(self.latest_pose)
        self.pose_pub.publish(out)

    def publish_viz(self):
        pts = self.acc.snapshot()
        if pts.shape[0]:
            self.viz_pub.publish(
                xyz_to_cloud(pts, "camera_init", self.get_clock().now().to_msg())
            )

    def save_snapshot(self):
        pts = self.acc.snapshot()
        if pts.shape[0]:
            write_pcd(os.path.join(SAVE_DIR, "current", "map.pcd"), pts)


def main():
    rclpy.init()
    rclpy.spin(VizBridge())


if __name__ == "__main__":
    main()
