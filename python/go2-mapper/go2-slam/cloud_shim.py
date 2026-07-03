#!/usr/bin/env python3
"""cloud_shim.py — make the Go2's deskewed cloud digestible by Point-LIO.

The Go2 publishes /utlidar/cloud_deskewed as a plain PointCloud2 with only
x,y,z,intensity (FLOAT32) — verified on hardware: no `ring`, no per-point
`time`. Point-LIO's stock PointCloud2 handlers (Ouster/Velodyne/Hesai) all
require ring+time and there is no generic-xyz path, so we repack each scan into
the exact layout Point-LIO's VELO16 handler consumes — velodyne_ros::Point:
x,y,z,intensity,time,ring — with time=0 and ring=0.

Why that's correct here: Unitree already motion-corrects (deskews) the cloud,
so there is no intra-scan timing to preserve; with time==0 the VELO16 handler
synthesizes a harmless yaw-based offset, and ring 0 (< scan_line) passes its
filter. This keeps the upstream Point-LIO fork completely unmodified — all the
Go2-specific adaptation lives here, in fast-to-iterate Python.
"""
import os

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSPresetProfiles
from sensor_msgs.msg import PointCloud2, PointField

IN_TOPIC = os.environ.get("LIDAR_TOPIC", "/utlidar/cloud_deskewed")
OUT_TOPIC = os.environ.get("SHIM_OUT_TOPIC", "/point_lio/cloud")

# velodyne_ros::Point wire layout Point-LIO expects (matched by field NAME by
# pcl::fromROSMsg, so a compact self-consistent packing is fine).
_OUT_STEP = 24
_OUT_FIELDS = [
    PointField(name="x", offset=0, datatype=PointField.FLOAT32, count=1),
    PointField(name="y", offset=4, datatype=PointField.FLOAT32, count=1),
    PointField(name="z", offset=8, datatype=PointField.FLOAT32, count=1),
    PointField(name="intensity", offset=12, datatype=PointField.FLOAT32, count=1),
    PointField(name="time", offset=16, datatype=PointField.FLOAT32, count=1),
    PointField(name="ring", offset=20, datatype=PointField.UINT16, count=1),
]
_OUT_DTYPE = np.dtype({
    "names": ["x", "y", "z", "intensity", "time", "ring"],
    "formats": ["<f4", "<f4", "<f4", "<f4", "<f4", "<u2"],
    "offsets": [0, 4, 8, 12, 16, 20],
    "itemsize": _OUT_STEP,
})

_NP = {  # PointField datatype -> numpy scalar
    PointField.INT8: "<i1", PointField.UINT8: "<u1",
    PointField.INT16: "<i2", PointField.UINT16: "<u2",
    PointField.INT32: "<i4", PointField.UINT32: "<u4",
    PointField.FLOAT32: "<f4", PointField.FLOAT64: "<f8",
}


def _read_field(raw, step, off, dt, n):
    """Read one named field out of a packed PointCloud2 buffer as float64."""
    view = np.frombuffer(raw, dtype=np.uint8).reshape(n, step)[:, off:off + np.dtype(dt).itemsize]
    return view.copy().view(dt).reshape(n).astype(np.float64)


class CloudShim(Node):
    def __init__(self):
        super().__init__("go2_cloud_shim")
        qos = QoSPresetProfiles.SENSOR_DATA.value
        self.pub = self.create_publisher(PointCloud2, OUT_TOPIC, qos)
        self.create_subscription(PointCloud2, IN_TOPIC, self.on_cloud, qos)
        self._warned = False
        self.get_logger().info(f"cloud_shim: {IN_TOPIC} -> {OUT_TOPIC} (velodyne layout)")

    def on_cloud(self, msg: PointCloud2):
        n = (len(msg.data) // msg.point_step) if msg.point_step else 0
        if n == 0:
            return
        off = {f.name: (f.offset, _NP.get(f.datatype)) for f in msg.fields}
        if "x" not in off or "y" not in off or "z" not in off:
            if not self._warned:
                self.get_logger().error(f"input cloud lacks x/y/z fields: {list(off)}")
                self._warned = True
            return
        raw = bytes(msg.data)
        out = np.zeros(n, dtype=_OUT_DTYPE)
        out["x"] = _read_field(raw, msg.point_step, *off["x"], n)
        out["y"] = _read_field(raw, msg.point_step, *off["y"], n)
        out["z"] = _read_field(raw, msg.point_step, *off["z"], n)
        if "intensity" in off and off["intensity"][1]:
            out["intensity"] = _read_field(raw, msg.point_step, *off["intensity"], n)
        # Per-point time: a tiny monotonic ramp (0 -> 1 ms). The Go2 cloud is
        # already deskewed, so the scan is effectively instantaneous — but with
        # time==0 Point-LIO's VELO16 handler *synthesizes* a spinning-Velodyne
        # timing (omega=3.61 deg/ms), which is wrong for the non-repetitive
        # Mid-360 and can diverge the filter. A small >0 ramp makes
        # given_offset_time=true so it uses these ~0 values as-is instead.
        out["time"] = np.linspace(0.0, 1e-3, n, dtype=np.float32)
        # ring stays 0 (single logical ring; < scan_line so it passes the filter)

        m = PointCloud2()
        m.header = msg.header
        m.height = 1
        m.width = n
        m.fields = _OUT_FIELDS
        m.is_bigendian = False
        m.point_step = _OUT_STEP
        m.row_step = _OUT_STEP * n
        m.is_dense = True
        m.data = out.tobytes()
        self.pub.publish(m)


def main():
    rclpy.init()
    rclpy.spin(CloudShim())


if __name__ == "__main__":
    main()
