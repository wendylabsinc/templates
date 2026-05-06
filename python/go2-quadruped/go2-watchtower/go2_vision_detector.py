#!/usr/bin/env python3
"""
go2_vision_detector.py

Phase 1 of the vision subsystem. Subscribes to the camera bridge's
`/go2/camera/compressed` JPEG stream, runs YOLOv11n constrained to the
COCO `person` class, and publishes:

    /go2/vision/raw_detections     vision_msgs/Detection2DArray
    /go2/vision/overlay/compressed sensor_msgs/CompressedImage  (JPEG, annotated)
    /go2/vision/health             std_msgs/Float32MultiArray   [fps, dropped, model, camera]

Inference runs in its own worker thread so ROS2 callbacks stay responsive.
The latest camera frame wins — older frames are dropped rather than
queued, since vision-stack consumers care about freshness, not history.

Tuning knobs are env-overridable (Phase 3 promotes these to vision.yaml):

    YOLO_MODEL          /app/models/yolo11n.pt
    YOLO_INPUT_SIZE     640      px (square)
    YOLO_CONF           0.5
    YOLO_IOU            0.45
    YOLO_OVERLAY_QUALITY 75      JPEG quality 1-100

NOTE on FPS: this image is CPU-only (ros:humble-ros-base-jammy). Expected
throughput on the Go2's Orin NX with 4 active cores at 15 W is ~5-10 FPS,
not the 20 FPS the spec calls for. GPU acceleration via TensorRT is a
Phase 3 base-image swap.
"""

import os
import threading
import time
from collections import deque

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import Float32MultiArray, MultiArrayDimension

try:
    from vision_msgs.msg import (
        Detection2D,
        Detection2DArray,
        ObjectHypothesisWithPose,
    )
except ImportError:
    Detection2DArray = None  # surfaced at runtime in main()


MODEL_PATH = os.environ.get("YOLO_MODEL", "/app/models/yolo11n.pt")
INPUT_SIZE = int(os.environ.get("YOLO_INPUT_SIZE", "640"))
CONF_THRESHOLD = float(os.environ.get("YOLO_CONF", "0.5"))
IOU_THRESHOLD = float(os.environ.get("YOLO_IOU", "0.45"))
OVERLAY_QUALITY = int(os.environ.get("YOLO_OVERLAY_QUALITY", "75"))
PERSON_CLASS_ID = 0  # COCO

# Health field positions (kept in one place so the layout JSON stays in sync).
HEALTH_LABELS = "fps,dropped_frames_last_sec,model_status,camera_status"
MODEL_STATUS_LOADING = 0.0
MODEL_STATUS_READY = 1.0
MODEL_STATUS_ERROR = 2.0
CAMERA_STATUS_NONE = 0.0
CAMERA_STATUS_OK = 1.0
CAMERA_STATUS_STALE = 2.0


class Go2VisionDetector(Node):
    def __init__(self):
        super().__init__("go2_vision_detector")

        if Detection2DArray is None:
            raise RuntimeError(
                "vision_msgs not importable — install ros-humble-vision-msgs in the image"
            )

        self.det_pub = self.create_publisher(
            Detection2DArray, "/go2/vision/raw_detections", 10
        )
        self.overlay_pub = self.create_publisher(
            CompressedImage, "/go2/vision/overlay/compressed", 10
        )
        self.health_pub = self.create_publisher(
            Float32MultiArray, "/go2/vision/health", 10
        )

        self.create_subscription(
            CompressedImage, "/go2/camera/compressed", self._on_frame, 10
        )
        self.create_timer(1.0, self._publish_health)

        self._lock = threading.Lock()
        self._latest: CompressedImage | None = None
        self._last_frame_mono: float | None = None

        # Rolling counters re-zeroed each health window.
        self._cam_frames_in_window = 0
        self._processed_in_window = 0
        self._window_start = time.monotonic()

        self._model = None
        self._model_status = MODEL_STATUS_LOADING

        self._stop = threading.Event()
        threading.Thread(target=self._inference_loop, daemon=True).start()

        self.get_logger().info(
            f"vision detector starting: model={MODEL_PATH}, size={INPUT_SIZE}, "
            f"conf={CONF_THRESHOLD}, iou={IOU_THRESHOLD}"
        )

    # -- ROS callbacks ------------------------------------------------------

    def _on_frame(self, msg: CompressedImage) -> None:
        with self._lock:
            self._latest = msg  # latest-frame-wins; old frame is dropped
            self._last_frame_mono = time.monotonic()
            self._cam_frames_in_window += 1

    def _publish_health(self) -> None:
        now = time.monotonic()
        with self._lock:
            window = max(1e-3, now - self._window_start)
            cam_frames = self._cam_frames_in_window
            processed = self._processed_in_window
            self._cam_frames_in_window = 0
            self._processed_in_window = 0
            self._window_start = now
            last_frame = self._last_frame_mono
            model_status = self._model_status

        cam_fps = cam_frames / window
        proc_fps = processed / window
        dropped = max(0.0, cam_fps - proc_fps)

        if last_frame is None:
            cam_status = CAMERA_STATUS_NONE
        elif (now - last_frame) > 2.0:
            cam_status = CAMERA_STATUS_STALE
        else:
            cam_status = CAMERA_STATUS_OK

        msg = Float32MultiArray()
        dim = MultiArrayDimension()
        dim.label = HEALTH_LABELS
        dim.size = 4
        dim.stride = 4
        msg.layout.dim.append(dim)
        msg.data = [proc_fps, dropped, model_status, cam_status]
        self.health_pub.publish(msg)

    # -- Worker thread ------------------------------------------------------

    def _inference_loop(self) -> None:
        try:
            from ultralytics import YOLO
            self._model = YOLO(MODEL_PATH)
            self._model_status = MODEL_STATUS_READY
            self.get_logger().info(f"YOLO model loaded from {MODEL_PATH}")
        except Exception as exc:
            self.get_logger().error(f"YOLO model load failed: {exc}")
            self._model_status = MODEL_STATUS_ERROR
            return

        while not self._stop.is_set():
            with self._lock:
                msg = self._latest
                self._latest = None  # consume

            if msg is None:
                # No new frame yet — small idle to avoid tight-spinning.
                time.sleep(0.005)
                continue

            try:
                self._process_frame(msg)
            except Exception as exc:
                self.get_logger().warning(f"inference failed: {exc}")
                # Brief backoff so a persistent failure doesn't melt the CPU.
                time.sleep(0.1)

    def _process_frame(self, msg: CompressedImage) -> None:
        arr = np.frombuffer(bytes(msg.data), dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            return

        results = self._model(
            bgr,
            imgsz=INPUT_SIZE,
            classes=[PERSON_CLASS_ID],
            conf=CONF_THRESHOLD,
            iou=IOU_THRESHOLD,
            verbose=False,
        )
        result = results[0]

        self._publish_detections(msg, result)
        self._publish_overlay(msg, result)

        with self._lock:
            self._processed_in_window += 1

    def _publish_detections(self, src_msg: CompressedImage, result) -> None:
        out = Detection2DArray()
        out.header = src_msg.header
        for box in result.boxes:
            xyxy = box.xyxy[0].cpu().numpy()
            x1, y1, x2, y2 = (float(v) for v in xyxy)
            conf = float(box.conf[0])
            cls = int(box.cls[0])

            det = Detection2D()
            det.header = src_msg.header
            det.bbox.center.position.x = (x1 + x2) / 2.0
            det.bbox.center.position.y = (y1 + y2) / 2.0
            det.bbox.size_x = x2 - x1
            det.bbox.size_y = y2 - y1

            hyp = ObjectHypothesisWithPose()
            hyp.hypothesis.class_id = str(cls)
            hyp.hypothesis.score = conf
            det.results.append(hyp)
            out.detections.append(det)
        self.det_pub.publish(out)

    def _publish_overlay(self, src_msg: CompressedImage, result) -> None:
        annotated = result.plot()  # BGR with default ultralytics styling
        ok, buf = cv2.imencode(
            ".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, OVERLAY_QUALITY]
        )
        if not ok:
            return
        out = CompressedImage()
        out.header = src_msg.header
        out.format = "jpeg"
        out.data = buf.tobytes()
        self.overlay_pub.publish(out)


def main():
    rclpy.init()
    if Detection2DArray is None:
        print(
            "go2_vision_detector: vision_msgs not available — apt install "
            "ros-humble-vision-msgs"
        )
        rclpy.try_shutdown()
        raise SystemExit(1)
    try:
        node = Go2VisionDetector()
    except Exception as exc:
        print(f"go2_vision_detector: init failed: {exc}")
        rclpy.try_shutdown()
        raise
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node._stop.set()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
