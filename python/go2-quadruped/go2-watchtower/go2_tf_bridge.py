#!/usr/bin/env python3
"""go2_tf_bridge.py

Republishes /utlidar/robot_pose as /tf (odom → base_link). The Unitree
lidar driver publishes the dog's odometry as a PoseStamped but doesn't
broadcast it to /tf, so Foxglove (and any rclpy consumer that wants
both lidar points and the dog body in the same scene) gets:

    Missing transform from frame <odom> to frame <base_link>

This bridge fixes it. Once it's running, /utlidar/cloud_deskewed (in
odom) and /go2/dog/marker (in base_link) coexist in the same 3D panel.
"""

import rclpy
from geometry_msgs.msg import PoseStamped, TransformStamped
from rclpy.node import Node
from tf2_ros import TransformBroadcaster


class Go2TfBridge(Node):
    def __init__(self) -> None:
        super().__init__("go2_tf_bridge")
        self._br = TransformBroadcaster(self)
        self.create_subscription(
            PoseStamped, "/utlidar/robot_pose", self._on_pose, 10
        )
        self.get_logger().info(
            "tf bridge: /utlidar/robot_pose → /tf (odom → base_link)"
        )

    def _on_pose(self, msg: PoseStamped) -> None:
        t = TransformStamped()
        t.header.stamp = msg.header.stamp
        t.header.frame_id = msg.header.frame_id or "odom"
        t.child_frame_id = "base_link"
        t.transform.translation.x = msg.pose.position.x
        t.transform.translation.y = msg.pose.position.y
        t.transform.translation.z = msg.pose.position.z
        t.transform.rotation = msg.pose.orientation
        self._br.sendTransform(t)


def main() -> None:
    rclpy.init()
    try:
        node = Go2TfBridge()
    except Exception as e:
        print(f"go2_tf_bridge: init failed: {e}", flush=True)
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
