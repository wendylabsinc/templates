#!/usr/bin/env python3
"""
go2_dog_marker.py

Publishes static markers in the dog's `base_link` frame for the Foxglove
3D panel:

    /go2/dog/marker        MarkerArray  body box + forward arrow (1 Hz)
    /go2/dog/follow_zone   MarkerArray  inner + outer follow-distance rings,
                                        recoloured live based on
                                        /go2/uwb/decision.follow_distance_status
                                        (1 Hz)

The body marker is the visual reference for "where is the dog?" — UWB
topics are in base_link, so without something at the origin the panel
just renders a tag dot in empty space.

The follow-zone markers paint the configured follow-distance band as two
concentric circles around the dog. Their color reflects whether the tag
is inside the band right now:
    ok        → green
    too_close → red
    too_far   → orange
    lost      → grey
"""

import json
import math

import rclpy
import os
from geometry_msgs.msg import Point
from rclpy.node import Node
from std_msgs.msg import String
from visualization_msgs.msg import Marker, MarkerArray


# Body marker tunables.
BODY_LENGTH = 0.70
BODY_WIDTH = 0.30
BODY_HEIGHT = 0.30
BODY_COLOR = (0.6, 0.6, 0.65, 0.75)
ARROW_COLOR = (1.0, 0.85, 0.10, 0.95)
ARROW_LENGTH = 0.5

# Follow-zone tunables — must match go2_uwb_filter.py defaults so the
# rings line up with the brain's notion of follow distance.
FOLLOW_MIN_M = float(os.environ.get("UWB_FOLLOW_MIN_M", "1.5"))
FOLLOW_MAX_M = float(os.environ.get("UWB_FOLLOW_MAX_M", "3.0"))
ZONE_LINE_WIDTH = 0.04
ZONE_HEIGHT = 0.05            # raise rings slightly off the ground for visibility
ZONE_SEGMENTS = 60            # smoothness of the circle line strip

ZONE_COLORS = {
    "ok":         (0.13, 0.77, 0.37, 0.85),  # green
    "too_close":  (0.94, 0.27, 0.27, 0.80),  # red
    "too_far":    (0.97, 0.62, 0.07, 0.80),  # orange
    "lost":       (0.40, 0.40, 0.45, 0.45),  # grey
}

PUBLISH_HZ = 1.0


class Go2DogMarker(Node):
    def __init__(self):
        super().__init__("go2_dog_marker")
        self.body_pub = self.create_publisher(MarkerArray, "/go2/dog/marker", 10)
        self.zone_pub = self.create_publisher(MarkerArray, "/go2/dog/follow_zone", 10)

        self.create_subscription(String, "/go2/uwb/decision", self._on_decision, 10)
        self.create_timer(1.0 / PUBLISH_HZ, self._publish)

        self._latest_status: str = "lost"
        self.get_logger().info(
            f"Go2 dog marker started; follow zone {FOLLOW_MIN_M:.1f}-{FOLLOW_MAX_M:.1f} m"
        )

    # -- ROS callbacks ------------------------------------------------------

    def _on_decision(self, msg: String) -> None:
        try:
            payload = json.loads(msg.data)
        except Exception:
            return
        status = payload.get("follow_distance_status", "lost")
        if status in ZONE_COLORS:
            self._latest_status = status

    def _publish(self) -> None:
        stamp = self.get_clock().now().to_msg()
        self._publish_body(stamp)
        self._publish_zone(stamp)

    # -- Markers ------------------------------------------------------------

    def _publish_body(self, stamp) -> None:
        body = Marker()
        body.header.frame_id = "base_link"
        body.header.stamp = stamp
        body.ns = "go2"
        body.id = 0
        body.type = Marker.CUBE
        body.action = Marker.ADD
        body.pose.orientation.w = 1.0
        body.scale.x = BODY_LENGTH
        body.scale.y = BODY_WIDTH
        body.scale.z = BODY_HEIGHT
        body.color.r, body.color.g, body.color.b, body.color.a = BODY_COLOR

        arrow = Marker()
        arrow.header.frame_id = "base_link"
        arrow.header.stamp = stamp
        arrow.ns = "go2"
        arrow.id = 1
        arrow.type = Marker.ARROW
        arrow.action = Marker.ADD
        arrow.pose.orientation.w = 1.0
        arrow.scale.x = ARROW_LENGTH
        arrow.scale.y = 0.06
        arrow.scale.z = 0.10
        arrow.color.r, arrow.color.g, arrow.color.b, arrow.color.a = ARROW_COLOR

        msg = MarkerArray()
        msg.markers = [body, arrow]
        self.body_pub.publish(msg)

    def _publish_zone(self, stamp) -> None:
        color = ZONE_COLORS[self._latest_status]
        msg = MarkerArray()
        msg.markers = [
            self._make_circle(stamp, FOLLOW_MIN_M, color, marker_id=10),
            self._make_circle(stamp, FOLLOW_MAX_M, color, marker_id=11),
        ]
        self.zone_pub.publish(msg)

    @staticmethod
    def _make_circle(
        stamp,
        radius: float,
        color: tuple[float, float, float, float],
        marker_id: int,
    ) -> Marker:
        m = Marker()
        m.header.frame_id = "base_link"
        m.header.stamp = stamp
        m.ns = "follow_zone"
        m.id = marker_id
        m.type = Marker.LINE_STRIP
        m.action = Marker.ADD
        m.pose.orientation.w = 1.0
        m.scale.x = ZONE_LINE_WIDTH
        m.color.r, m.color.g, m.color.b, m.color.a = color
        for i in range(ZONE_SEGMENTS + 1):
            angle = 2 * math.pi * i / ZONE_SEGMENTS
            p = Point()
            p.x = radius * math.cos(angle)
            p.y = radius * math.sin(angle)
            p.z = ZONE_HEIGHT
            m.points.append(p)
        return m


def main():
    rclpy.init()
    try:
        node = Go2DogMarker()
    except Exception as exc:
        print(f"go2_dog_marker: init failed: {exc}")
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
