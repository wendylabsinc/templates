#!/usr/bin/env python3
"""
go2_uwb_bridge.py

Subscribes to the Go2's `/uwbstate` (unitree_go/UwbState) and republishes
human-friendly derivatives. Phase 1 of the UWB subsystem: no filtering, no
tracking state machine — just legible numbers + a 3D point so Foxglove can
render "the tag is X m on the left at Y°" at a glance.

Topics published (all in the dog's `base_link` frame):

    /go2/uwb/point        geometry_msgs/PointStamped     tag in (x, y, 0)
    /go2/uwb/pose         geometry_msgs/PoseStamped      same, with yaw orientation
    /go2/uwb/range        std_msgs/Float32               meters
    /go2/uwb/bearing_deg  std_msgs/Float32               degrees, +ve = left
    /go2/uwb/info         std_msgs/String                "tag at 3.20 m, 15.4° left ..."
    /go2/uwb/health       std_msgs/Float32MultiArray     [is_seen, last_seen_ms, error_state, hz]

Frame convention assumed: REP-103 in base_link — yaw=0 → ahead, +π/2 → left,
−π/2 → right. If the Go2 reports yaw_est with a different sign convention,
flip the sign in `_compute_xy` (one line).
"""

import math
import time
from threading import Lock

import rclpy
from geometry_msgs.msg import PointStamped, PoseStamped
from rclpy.node import Node
from std_msgs.msg import Float32, Float32MultiArray, MultiArrayDimension, String

try:
    from unitree_go.msg import UwbState
except ImportError:
    UwbState = None  # surfaced at runtime in main()


HEALTH_RATE_HZ = 1.0
SEEN_TIMEOUT_S = 0.5  # tag is "seen" if a msg arrived in the last 500 ms


def _compute_xy(range_m: float, yaw_rad: float) -> tuple[float, float]:
    return range_m * math.cos(yaw_rad), range_m * math.sin(yaw_rad)


class Go2UwbBridge(Node):
    def __init__(self):
        super().__init__("go2_uwb_bridge")

        self.point_pub = self.create_publisher(PointStamped, "/go2/uwb/point", 10)
        self.pose_pub = self.create_publisher(PoseStamped, "/go2/uwb/pose", 10)
        self.range_pub = self.create_publisher(Float32, "/go2/uwb/range", 10)
        self.bearing_pub = self.create_publisher(Float32, "/go2/uwb/bearing_deg", 10)
        self.info_pub = self.create_publisher(String, "/go2/uwb/info", 10)
        self.health_pub = self.create_publisher(Float32MultiArray, "/go2/uwb/health", 10)

        self.create_subscription(UwbState, "/uwbstate", self._on_uwb, 10)
        self.create_timer(1.0 / HEALTH_RATE_HZ, self._publish_health)

        self._lock = Lock()
        self._last_msg_time: float | None = None
        self._msg_count = 0
        self._rate_window_start = time.monotonic()
        self._error_state = 0
        self._enabled_from_app = 0
        # Diagnostic state for stdout logs. We log the first /uwbstate we
        # see, every change in the gating fields (so `wendy logs` reveals
        # *why* we're silent), and a 1 Hz heartbeat alongside the health
        # publish.
        self._first_msg_logged = False
        self._last_gating: tuple[int, int] | None = None
        self._last_range_m = 0.0
        self._last_bearing_deg = 0.0
        self._published_count = 0

        self.get_logger().info(
            "Go2 UWB bridge started; subscribing to /uwbstate, "
            "publishing /go2/uwb/{point,pose,range,bearing_deg,info,health}"
        )

    def _on_uwb(self, msg) -> None:
        now_mono = time.monotonic()
        with self._lock:
            self._last_msg_time = now_mono
            self._msg_count += 1
            self._error_state = int(msg.error_state)
            self._enabled_from_app = int(msg.enabled_from_app)

        # First-arrival log + edge-triggered logs on gating changes. The
        # /info topic also carries the gated reason for Foxglove; the
        # stdout warn here is for `wendy logs` so the supervisor surfaces
        # "user closed the Unitree app" or "UWB module faulted" without
        # needing Foxglove open.
        gating = (int(msg.error_state), int(msg.enabled_from_app))
        if not self._first_msg_logged:
            self._first_msg_logged = True
            self.get_logger().info(
                f"first /uwbstate received: distance_est={float(msg.distance_est):.2f} m, "
                f"yaw_est={math.degrees(float(msg.yaw_est)):+.1f}°, "
                f"err=0x{gating[0]:02x}, app={gating[1]}"
            )
        elif self._last_gating is not None and self._last_gating != gating:
            prev_err, prev_app = self._last_gating
            cur_err, cur_app = gating
            if prev_app != cur_app:
                self.get_logger().warn(
                    f"enabled_from_app: {prev_app} → {cur_app} "
                    f"({'follow-mode ON' if cur_app == 1 else 'follow-mode OFF — gating /point'})"
                )
            if prev_err != cur_err:
                tail = " (FAULT)" if cur_err != 0 else " (cleared)"
                self.get_logger().warn(
                    f"error_state: 0x{prev_err:02x} → 0x{cur_err:02x}{tail}"
                )
        self._last_gating = gating

        # Don't republish a "tag locked" position when the UWB module
        # itself is reporting a fault, or when the app-side enable bit
        # is off (e.g. user closed the Unitree app). Health topic still
        # carries the raw fields for diagnostics.
        if self._error_state != 0 or self._enabled_from_app != 1:
            self._publish_info(
                f"uwb gated (err=0x{self._error_state:02x}, "
                f"app={self._enabled_from_app})"
            )
            return

        range_m = float(msg.distance_est)
        # The Go2's UWB receiver reports yaw_est with a 180° offset from
        # REP-103 (yaw=0 means directly BEHIND the dog, not ahead — likely
        # because the UWB module is physically mounted facing rearward).
        # Rotate by π so downstream topics use the standard convention:
        # bearing 0° = ahead, +90° = left, ±180° = behind.
        yaw_rad = float(msg.yaw_est) + math.pi
        # Normalize to [-π, π]
        yaw_rad = math.atan2(math.sin(yaw_rad), math.cos(yaw_rad))
        stamp = self.get_clock().now().to_msg()

        if range_m <= 0.0:
            # Tag not currently locked. Don't pollute /point and /pose with
            # zeros — those are visualised in 3D and would jump to the origin.
            # Still emit /info for visibility.
            self._publish_info("no tag (range=0)")
            return

        x, y = _compute_xy(range_m, yaw_rad)
        bearing_deg = math.degrees(yaw_rad)

        with self._lock:
            self._last_range_m = range_m
            self._last_bearing_deg = bearing_deg
            self._published_count += 1

        pt = PointStamped()
        pt.header.stamp = stamp
        pt.header.frame_id = "base_link"
        pt.point.x = x
        pt.point.y = y
        pt.point.z = 0.0
        self.point_pub.publish(pt)

        pose = PoseStamped()
        pose.header = pt.header
        pose.pose.position.x = x
        pose.pose.position.y = y
        pose.pose.position.z = 0.0
        # quaternion from yaw_rad about z: (0, 0, sin(θ/2), cos(θ/2))
        pose.pose.orientation.z = math.sin(yaw_rad / 2.0)
        pose.pose.orientation.w = math.cos(yaw_rad / 2.0)
        self.pose_pub.publish(pose)

        rmsg = Float32(); rmsg.data = range_m; self.range_pub.publish(rmsg)
        bmsg = Float32(); bmsg.data = bearing_deg; self.bearing_pub.publish(bmsg)

        side = "left" if bearing_deg > 0.5 else ("right" if bearing_deg < -0.5 else "ahead")
        self._publish_info(
            f"tag at {range_m:.2f} m, {abs(bearing_deg):.1f}° {side} "
            f"(err=0x{self._error_state:02x}, app={self._enabled_from_app})"
        )

    def _publish_info(self, text: str) -> None:
        msg = String()
        msg.data = text
        self.info_pub.publish(msg)

    def _publish_health(self) -> None:
        with self._lock:
            now = time.monotonic()
            last = self._last_msg_time
            count = self._msg_count
            window = now - self._rate_window_start
            self._msg_count = 0
            self._rate_window_start = now
            err = self._error_state
            app = self._enabled_from_app
            range_m = self._last_range_m
            bearing_deg = self._last_bearing_deg
            published = self._published_count

        if last is None:
            is_seen = 0.0
            last_seen_ms = -1.0
        else:
            age = now - last
            is_seen = 1.0 if age < SEEN_TIMEOUT_S else 0.0
            last_seen_ms = age * 1000.0

        hz = count / window if window > 0 else 0.0

        msg = Float32MultiArray()
        dim = MultiArrayDimension()
        dim.label = "is_seen,last_seen_ms,error_state,hz"
        dim.size = 4
        dim.stride = 4
        msg.layout.dim.append(dim)
        msg.data = [is_seen, last_seen_ms, float(err), hz]
        self.health_pub.publish(msg)

        # Stdout heartbeat at the same 1 Hz cadence — visible in
        # `wendy logs`, no Foxglove needed.
        if last is None:
            self.get_logger().info("hb: no /uwbstate yet (waiting for first sample)")
        else:
            gating_str = (
                "ok" if (err == 0 and app == 1)
                else f"gated(err=0x{err:02x},app={app})"
            )
            self.get_logger().info(
                f"hb: {hz:.1f} Hz, last_seen={last_seen_ms:.0f} ms, "
                f"range={range_m:.2f} m, bearing={bearing_deg:+.1f}°, "
                f"published={published}, {gating_str}"
            )


def main():
    rclpy.init()
    if UwbState is None:
        print(
            "go2_uwb_bridge: unitree_go.msg.UwbState not importable — "
            "is the unitree_ros2 workspace sourced?"
        )
        rclpy.try_shutdown()
        raise SystemExit(1)
    try:
        node = Go2UwbBridge()
    except Exception as e:
        print(f"go2_uwb_bridge: init failed: {e}")
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
