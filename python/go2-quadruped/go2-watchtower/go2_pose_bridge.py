#!/usr/bin/env python3
"""
go2_pose_bridge.py

Mirrors `/sportmodestate` (`unitree_go/msg/SportModeState`) into a
flat `std_msgs/String` JSON on `/go2/dog/pose_json` so go2-brain can
consume world-frame body pose without linking the Unitree IDL.

Schema (one message per inbound SportModeState, capped at PUBLISH_HZ):

    {
      "stamp_ns": int,        # publish-time timestamp
      "x_m":      float,      # world-frame x  (sportmodestate.position[0])
      "y_m":      float,      # world-frame y  (sportmodestate.position[1])
      "yaw_rad":  float       # body yaw       (sportmodestate.imu_state.rpy[2])
    }

The world frame's origin is whatever sportmodestate decided at boot —
the brain only uses *relative* positions for ghost-trail anchoring, so
the choice of origin doesn't matter as long as it's consistent across a
recovery cycle (which it is, since the dog doesn't reset sportmodestate
mid-run).

Throttled to PUBLISH_HZ (default 20 Hz) to match the watchtower's other
brain-facing publishers; sportmodestate often arrives faster than that.
"""

import json
import os
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

try:
    from unitree_go.msg import SportModeState
except ImportError:  # pragma: no cover — surfaced at runtime in main()
    SportModeState = None  # type: ignore[assignment]


PUBLISH_HZ = float(os.environ.get("POSE_BRIDGE_HZ", "20"))
TOPIC_OUT = os.environ.get("POSE_BRIDGE_TOPIC", "/go2/dog/pose_json")
TOPIC_IN = os.environ.get("POSE_BRIDGE_INPUT", "/sportmodestate")


class Go2PoseBridge(Node):
    def __init__(self):
        super().__init__("go2_pose_bridge")

        self.pub = self.create_publisher(String, TOPIC_OUT, 10)
        self.create_subscription(SportModeState, TOPIC_IN, self._on_state, 10)

        self._latest: tuple[float, float, float] | None = None  # (x, y, yaw)
        self._last_publish: float = 0.0
        self._period_s = 1.0 / max(PUBLISH_HZ, 0.1)
        self.create_timer(self._period_s, self._publish)

        self.get_logger().info(
            f"pose bridge: {TOPIC_IN} → {TOPIC_OUT} @ {PUBLISH_HZ:.1f} Hz"
        )

    def _on_state(self, msg: "SportModeState") -> None:
        try:
            x = float(msg.position[0])
            y = float(msg.position[1])
            yaw = float(msg.imu_state.rpy[2])
        except (AttributeError, IndexError) as exc:
            self.get_logger().warning(f"sportmodestate missing fields: {exc}")
            return
        self._latest = (x, y, yaw)

    def _publish(self) -> None:
        latest = self._latest
        if latest is None:
            return
        x, y, yaw = latest
        payload = {
            "stamp_ns": time.time_ns(),
            "x_m": round(x, 4),
            "y_m": round(y, 4),
            "yaw_rad": round(yaw, 5),
        }
        msg = String()
        msg.data = json.dumps(payload, separators=(",", ":"))
        self.pub.publish(msg)


def main():
    rclpy.init()
    if SportModeState is None:
        print(
            "go2_pose_bridge: unitree_go not importable — pose JSON disabled. "
            "This is expected outside the Watchtower container; the brain's "
            "ghost trail will fall back to today's blind-spin recovery."
        )
        rclpy.try_shutdown()
        raise SystemExit(1)
    try:
        node = Go2PoseBridge()
    except Exception as exc:
        print(f"go2_pose_bridge: init failed: {exc}")
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
