#!/usr/bin/env python3
"""
go2_video_bridge.py

Bridges the Go2 front camera (delivered by the robot's onboard service over
WebRTC) to a ROS2 `sensor_msgs/CompressedImage` topic so Foxglove sees it like
any other camera feed.

Topic published: /go2/camera/compressed  (sensor_msgs/CompressedImage, JPEG)
"""

import asyncio
import logging
import os
import queue
import threading

import cv2
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage

from unitree_webrtc_connect import (
    UnitreeWebRTCConnection,
    WebRTCConnectionMethod,
)

GO2_IP = os.environ.get("GO2_IP", "{{.GO2_IP}}")
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "80"))
PUBLISH_HZ = int(os.environ.get("PUBLISH_HZ", "30"))
TOPIC = os.environ.get("CAMERA_TOPIC", "/go2/camera/compressed")
KEYFRAME_REQUEST_INTERVAL_S = 3.0  # Re-request a keyframe this often until decoding starts.

# aiortc spams "H264Decoder() failed to decode" until the first I-frame arrives;
# upstream's own example silences this the same way.
logging.getLogger("aiortc.codecs.h264").setLevel(logging.ERROR)


class Go2VideoBridge(Node):
    def __init__(self):
        super().__init__("go2_video_bridge")
        self.pub = self.create_publisher(CompressedImage, TOPIC, 10)
        self.frames: "queue.Queue" = queue.Queue(maxsize=1)
        self._first_frame_logged = False
        self._conn: UnitreeWebRTCConnection | None = None

        threading.Thread(target=self._run_webrtc, daemon=True).start()

        self.create_timer(1.0 / PUBLISH_HZ, self._publish_latest)
        self.get_logger().info(
            f"Connecting to Go2 at {GO2_IP}, publishing to {TOPIC}"
        )

    def _run_webrtc(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._webrtc_main())
        except Exception as e:
            self.get_logger().error(f"WebRTC thread crashed: {e}")

    async def _webrtc_main(self):
        self._conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=GO2_IP)
        await self._conn.connect()
        self._conn.video.switchVideoChannel(True)
        self._conn.video.add_track_callback(self._on_track)
        # Periodically poke the encoder for a keyframe until we've decoded one;
        # without this, aiortc's H264 decoder can stay wedged on a partial GOP.
        asyncio.create_task(self._keyframe_nag())
        while True:
            await asyncio.sleep(1)

    async def _keyframe_nag(self):
        while not self._first_frame_logged:
            await asyncio.sleep(KEYFRAME_REQUEST_INTERVAL_S)
            if self._first_frame_logged or self._conn is None:
                return
            self._request_keyframe()

    def _request_keyframe(self):
        # Walk the peer connection's video receivers and send PLI (RTCP Picture
        # Loss Indication). aiortc keeps this on the receiver as a private hook;
        # if upstream renames it, fall back to no-op rather than crash.
        try:
            pc = self._conn.pc
            for transceiver in pc.getTransceivers():
                receiver = transceiver.receiver
                track = getattr(receiver, "track", None)
                if track is None or track.kind != "video":
                    continue
                send_pli = getattr(receiver, "_send_rtcp_pli", None)
                ssrc = getattr(receiver, "_ssrc", None) or getattr(receiver, "_track_id", None)
                if send_pli and ssrc is not None:
                    asyncio.ensure_future(send_pli(ssrc))
                    self.get_logger().info("Requested H264 keyframe (PLI)")
        except Exception as e:
            self.get_logger().warning(f"Keyframe request failed: {e}")

    async def _on_track(self, track):
        # aiortc raises MediaStreamError with an empty message when the
        # track ends (most common cause: phone Go2 app stole the WebRTC
        # connection — only one client allowed). Without backoff this
        # loops at thousands of iterations per second and floods the log.
        # Break out so the supervisor can restart us cleanly.
        consecutive_errors = 0
        while True:
            try:
                frame = await track.recv()
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                # rclpy's logger doesn't accept printf-style args; format
                # into a single string before calling .warning/.error.
                if consecutive_errors == 1:
                    self.get_logger().warning(
                        f"track.recv() error ({type(e).__name__}); "
                        f"track likely dead — phone Go2 app open?"
                    )
                if consecutive_errors >= 5:
                    self.get_logger().error(
                        f"track.recv() failed {consecutive_errors} times "
                        f"in a row; killing process so supervisor restarts "
                        f"the bridge with a fresh WebRTC handshake"
                    )
                    # Hard-exit (not just `return`) — `return` from an async
                    # callback only ends this coroutine; the main rclpy.spin()
                    # keeps running and the supervisor never notices the
                    # bridge is dead. os._exit bypasses cleanup; that's
                    # intentional — the supervisor will start us fresh.
                    os._exit(1)
                await asyncio.sleep(0.1)
                continue
            img = frame.to_ndarray(format="bgr24")
            if not self._first_frame_logged:
                self._first_frame_logged = True
                self.get_logger().info(
                    f"First video frame decoded: {img.shape[1]}x{img.shape[0]}"
                )
            try:
                self.frames.get_nowait()
            except queue.Empty:
                pass
            self.frames.put_nowait(img)

    def _publish_latest(self):
        try:
            img = self.frames.get_nowait()
        except queue.Empty:
            return
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            return
        msg = CompressedImage()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = "go2_camera"
        msg.format = "jpeg"
        msg.data = buf.tobytes()
        self.pub.publish(msg)


def main():
    rclpy.init()
    node = Go2VideoBridge()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
