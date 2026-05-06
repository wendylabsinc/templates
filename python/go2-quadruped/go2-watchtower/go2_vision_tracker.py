#!/usr/bin/env python3
"""
go2_vision_tracker.py

Phase 2 of the vision subsystem. Subscribes to `/go2/vision/raw_detections`
(the unfiltered detector output from Phase 1) and the camera frames on
`/go2/camera/compressed`, runs **BYTETrack** via the `supervision` library
to assign persistent track IDs across frames, estimates per-track distance
from bounding-box height assuming a 1.7 m person, and publishes:

    /go2/vision/tracked_persons             vision_msgs/Detection2DArray
    /go2/vision/tracked_persons_json        std_msgs/String (JSON; brain's wire format)
    /go2/vision/tracked_overlay/compressed  sensor_msgs/CompressedImage
    /go2/vision/tracks_summary              std_msgs/String  (human-readable)

How extra fields are packed into Detection2DArray (no custom msg in Phase 2):

    detection.id                          str(track_id)
    detection.bbox.{center,size_x,size_y} bounding box
    result.hypothesis.{class_id,score}    "0" + confidence
    result.pose.pose.position.x           age_frames (float)
    result.pose.pose.position.y           distance_confidence (0..1)
    result.pose.pose.position.z           est_distance_m

The `tracked_persons_json` mirror is what go2-brain consumes. It carries
the same per-track data plus a precomputed `bearing_deg` so the brain
doesn't need to know `VISION_FY` / `VISION_IMAGE_WIDTH`. Schema:

    {
      "stamp_ns": int,
      "image_age_ms": int,
      "tracks": [
        {
          "id": str, "bearing_deg": float, "distance_m": float,
          "dist_confidence": float, "age_frames": int,
          "score": float, "bbox_h_px": float
        }, ...
      ]
    }

Tunables (Phase 3 → vision.yaml):

    VISION_FY                  vertical focal length (px). Placeholder default
                               until calibration; expect inaccurate distances
                               with the default — see the calibration script
                               in Phase 3.
    VISION_IMAGE_WIDTH         camera horizontal pixel count (used for the
                               JSON publisher's bearing computation).
    VISION_PERSON_HEIGHT_M     1.7 — assumed real-world person height.
    VISION_EDGE_MARGIN_PX      10 — bbox-to-frame-edge margin below which
                               distance_confidence drops.
    BYTETRACK_THRESH           0.5 — detection score gating for ByteTrack.
    BYTETRACK_MATCH_THRESH     0.8 — IoU match threshold.
    BYTETRACK_BUFFER_FRAMES    30  — frames a lost track is kept alive
                               before being dropped (~1 s @ 30 fps).
    YOLO_OVERLAY_QUALITY       75  — JPEG quality for the annotated overlay.
"""

import json
import math
import os
import threading
import time

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import Float32MultiArray, MultiArrayDimension, String
from vision_msgs.msg import (
    Detection2D,
    Detection2DArray,
    ObjectHypothesisWithPose,
)

try:
    import supervision as sv
except ImportError:
    sv = None  # surfaced at runtime in main()


# --- Tunables ---------------------------------------------------------------
PERSON_HEIGHT_M = float(os.environ.get("VISION_PERSON_HEIGHT_M", "1.7"))
# Vertical focal length (px). 615 is a placeholder for a 720p camera with
# ~60° vertical FOV; calibrate properly in Phase 3.
FOCAL_Y_PX = float(os.environ.get("VISION_FY", "615"))
# Horizontal pixel count of the source camera. Must match what the
# detector and uwb_filter use, otherwise bearings will be off.
IMAGE_WIDTH_PX = int(os.environ.get("VISION_IMAGE_WIDTH", "1280"))
EDGE_MARGIN_PX = int(os.environ.get("VISION_EDGE_MARGIN_PX", "10"))
EDGE_FADE_PX = int(os.environ.get("VISION_EDGE_FADE_PX", "60"))
BYTETRACK_THRESH = float(os.environ.get("BYTETRACK_THRESH", "0.5"))
BYTETRACK_MATCH_THRESH = float(os.environ.get("BYTETRACK_MATCH_THRESH", "0.8"))
BYTETRACK_BUFFER = int(os.environ.get("BYTETRACK_BUFFER_FRAMES", "30"))
OVERLAY_QUALITY = int(os.environ.get("YOLO_OVERLAY_QUALITY", "75"))
# ---------------------------------------------------------------------------

# Stable per-track palette (BGR for cv2 drawing).
_PALETTE_BGR = [
    (255, 56, 56), (56, 255, 56), (56, 56, 255),
    (255, 255, 56), (255, 56, 255), (56, 255, 255),
    (255, 128, 56), (128, 56, 255), (56, 255, 128),
    (200, 200, 200),
]


def _color_for_track(tid: int) -> tuple[int, int, int]:
    return _PALETTE_BGR[tid % len(_PALETTE_BGR)]


def _distance_confidence(y1: float, y2: float, img_h: int) -> float:
    """1.0 when the bbox is fully in frame with healthy margins; fades to
    0.3 when the top or bottom touches the image edge (person clipped →
    distance estimate is suspect)."""
    top_margin = y1
    bottom_margin = img_h - y2
    if top_margin < EDGE_MARGIN_PX or bottom_margin < EDGE_MARGIN_PX:
        return 0.3
    min_margin = min(top_margin, bottom_margin)
    return min(1.0, max(0.3, min_margin / EDGE_FADE_PX))


class Go2VisionTracker(Node):
    def __init__(self):
        super().__init__("go2_vision_tracker")

        if sv is None:
            raise RuntimeError(
                "supervision not importable — pip install supervision in the image"
            )

        self.tracked_pub = self.create_publisher(
            Detection2DArray, "/go2/vision/tracked_persons", 10
        )
        # JSON mirror — go2-brain consumes this directly so it doesn't
        # need vision_msgs IDLs in its CycloneDDS-only setup.
        self.tracked_json_pub = self.create_publisher(
            String, "/go2/vision/tracked_persons_json", 10
        )
        self.overlay_pub = self.create_publisher(
            CompressedImage, "/go2/vision/tracked_overlay/compressed", 10
        )
        self.summary_pub = self.create_publisher(
            String, "/go2/vision/tracks_summary", 10
        )
        self.health_pub = self.create_publisher(
            Float32MultiArray, "/go2/vision/tracker_health", 10
        )

        self.create_subscription(
            Detection2DArray, "/go2/vision/raw_detections",
            self._on_detections, 10,
        )
        self.create_subscription(
            CompressedImage, "/go2/camera/compressed",
            self._on_frame, 10,
        )
        self.create_timer(1.0, self._publish_health)
        self._processed_in_window = 0
        self._window_start = time.monotonic()
        self._active_tracks = 0

        self._tracker = sv.ByteTrack(
            track_activation_threshold=BYTETRACK_THRESH,
            lost_track_buffer=BYTETRACK_BUFFER,
            minimum_matching_threshold=BYTETRACK_MATCH_THRESH,
        )
        self._lock = threading.Lock()
        self._latest_frame: CompressedImage | None = None
        self._frame_count = 0
        self._track_first_seen: dict[int, int] = {}

        self.get_logger().info(
            f"vision tracker started: fy={FOCAL_Y_PX} px, "
            f"person_height={PERSON_HEIGHT_M} m, buffer={BYTETRACK_BUFFER} frames, "
            f"track_thresh={BYTETRACK_THRESH}, match_thresh={BYTETRACK_MATCH_THRESH}"
        )

    # -- ROS callbacks ------------------------------------------------------

    def _on_frame(self, msg: CompressedImage) -> None:
        with self._lock:
            self._latest_frame = msg

    def _on_detections(self, msg: Detection2DArray) -> None:
        sv_detections = self._to_supervision(msg)

        # ByteTrack must be ticked on every detection callback (even with an
        # empty input) to age out lost tracks and maintain internal state.
        try:
            tracked = self._tracker.update_with_detections(sv_detections)
        except Exception as exc:
            self.get_logger().warning(f"tracker update failed: {exc}")
            return

        with self._lock:
            self._frame_count += 1
            current_frame = self._frame_count
            ages = self._update_ages(tracked, current_frame)
            self._processed_in_window += 1
            self._active_tracks = (
                len(tracked) if tracked.tracker_id is not None else 0
            )

        self._publish_tracked(msg.header, tracked, ages)
        self._publish_summary(tracked, ages)
        self._publish_overlay(msg.header, tracked, ages)

    # -- Conversion helpers -------------------------------------------------

    @staticmethod
    def _to_supervision(msg: Detection2DArray) -> "sv.Detections":
        if not msg.detections:
            return sv.Detections.empty()
        xyxy = []
        confidence = []
        class_id = []
        for d in msg.detections:
            cx = d.bbox.center.position.x
            cy = d.bbox.center.position.y
            sx = d.bbox.size_x
            sy = d.bbox.size_y
            xyxy.append([cx - sx / 2, cy - sy / 2, cx + sx / 2, cy + sy / 2])
            if d.results:
                conf = float(d.results[0].hypothesis.score)
                cls_str = d.results[0].hypothesis.class_id
                cls = int(cls_str) if cls_str.isdigit() else 0
            else:
                conf = 0.0
                cls = 0
            confidence.append(conf)
            class_id.append(cls)
        return sv.Detections(
            xyxy=np.array(xyxy, dtype=float),
            confidence=np.array(confidence, dtype=float),
            class_id=np.array(class_id, dtype=int),
        )

    def _update_ages(self, tracked: "sv.Detections", current_frame: int) -> list[int]:
        ages: list[int] = []
        if tracked.tracker_id is None or len(tracked) == 0:
            return ages
        active_ids: set[int] = set()
        for tid in tracked.tracker_id:
            if tid is None:
                ages.append(0)
                continue
            tid_i = int(tid)
            active_ids.add(tid_i)
            if tid_i not in self._track_first_seen:
                self._track_first_seen[tid_i] = current_frame
            ages.append(current_frame - self._track_first_seen[tid_i])
        # Lazy GC: drop first-seen entries for tracks that have aged out far
        # past the byte-track buffer so the dict can't grow unbounded.
        if len(self._track_first_seen) > 256:
            self._track_first_seen = {
                k: v for k, v in self._track_first_seen.items() if k in active_ids
            }
        return ages

    # -- Publishers ---------------------------------------------------------

    def _publish_tracked(
        self,
        header,
        tracked: "sv.Detections",
        ages: list[int],
    ) -> None:
        out = Detection2DArray()
        out.header = header

        # We need image height for distance_confidence. Pull from latest frame
        # (close-enough; the bbox came from a frame within ~1 inference period).
        with self._lock:
            frame = self._latest_frame
        img_h = self._frame_height(frame) if frame is not None else 0

        json_tracks: list[dict] = []
        cx_image = IMAGE_WIDTH_PX / 2.0

        if tracked.tracker_id is None:
            self.tracked_pub.publish(out)
            self._publish_tracked_json(json_tracks)
            return

        for i in range(len(tracked)):
            tid = tracked.tracker_id[i]
            if tid is None:
                continue
            x1, y1, x2, y2 = (float(v) for v in tracked.xyxy[i])
            bbox_h = max(1.0, y2 - y1)
            conf = float(tracked.confidence[i]) if tracked.confidence is not None else 0.0
            est_dist = FOCAL_Y_PX * PERSON_HEIGHT_M / bbox_h
            dist_conf = _distance_confidence(y1, y2, img_h) if img_h > 0 else 0.5
            age = ages[i] if i < len(ages) else 0

            det = Detection2D()
            det.header = header
            det.id = str(int(tid))
            det.bbox.center.position.x = (x1 + x2) / 2.0
            det.bbox.center.position.y = (y1 + y2) / 2.0
            det.bbox.size_x = x2 - x1
            det.bbox.size_y = y2 - y1

            hyp = ObjectHypothesisWithPose()
            hyp.hypothesis.class_id = "0"
            hyp.hypothesis.score = conf
            # Side-channel scalars (see module docstring for the contract).
            hyp.pose.pose.position.x = float(age)
            hyp.pose.pose.position.y = float(dist_conf)
            hyp.pose.pose.position.z = float(est_dist)
            det.results.append(hyp)
            out.detections.append(det)

            # Same convention as go2_uwb_filter._on_tracks: pixel left of
            # centre → +ve bearing (REP-103). Brain consumes this directly.
            cx_pixel = (x1 + x2) / 2.0
            bearing_rad = math.atan2(cx_image - cx_pixel, FOCAL_Y_PX)
            bearing_deg = math.degrees(bearing_rad)
            json_tracks.append(
                {
                    "id": str(int(tid)),
                    "bearing_deg": round(bearing_deg, 2),
                    "distance_m": round(est_dist, 3),
                    "dist_confidence": round(dist_conf, 3),
                    "age_frames": int(age),
                    "score": round(conf, 3),
                    "bbox_h_px": round(bbox_h, 1),
                }
            )

        self.tracked_pub.publish(out)
        self._publish_tracked_json(json_tracks, header=header)

    def _publish_tracked_json(self, tracks: list[dict], header=None) -> None:
        """Mirror of /go2/vision/tracked_persons in flat JSON for go2-brain.
        Fired on every detector tick — empty `tracks` is published rather
        than skipped, so a no-detection frame still tells the brain
        "vision is alive but sees nothing"."""
        # Image age: header.stamp is when the *frame* was captured.
        # Compare against now (rclpy clock) to give the brain a hint about
        # how stale the bbox is. `stamp_ns` itself is monotonic at-publish.
        now_ns = time.time_ns()
        if header is not None and hasattr(header, "stamp"):
            stamp = header.stamp
            frame_ns = int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
            image_age_ms = max(0, (now_ns - frame_ns) // 1_000_000) if frame_ns else 0
        else:
            image_age_ms = 0
        payload = {
            "stamp_ns": now_ns,
            "image_age_ms": int(image_age_ms),
            "tracks": tracks,
        }
        msg = String()
        msg.data = json.dumps(payload, separators=(",", ":"))
        self.tracked_json_pub.publish(msg)

    def _publish_summary(self, tracked: "sv.Detections", ages: list[int]) -> None:
        msg = String()
        if tracked.tracker_id is None or len(tracked) == 0:
            msg.data = "no tracks"
            self.summary_pub.publish(msg)
            return
        lines = []
        for i in range(len(tracked)):
            tid = tracked.tracker_id[i]
            if tid is None:
                continue
            x1, y1, x2, y2 = (float(v) for v in tracked.xyxy[i])
            cx = (x1 + x2) / 2.0
            bbox_h = max(1.0, y2 - y1)
            est_dist = FOCAL_Y_PX * PERSON_HEIGHT_M / bbox_h
            conf = float(tracked.confidence[i]) if tracked.confidence is not None else 0.0
            age = ages[i] if i < len(ages) else 0
            # Side guess relative to the camera's horizontal centre. We
            # don't know image width here without the frame; bake a 640 px
            # default and let the human reader treat it as approximate.
            side = "ahead"
            if cx < 256:
                side = "left"
            elif cx > 384:
                side = "right"
            lines.append(
                f"#{int(tid)} {est_dist:.2f} m {side}, conf={conf:.2f}, age={age}"
            )
        msg.data = "\n".join(lines) if lines else "no tracks"
        self.summary_pub.publish(msg)

    def _publish_overlay(
        self,
        header,
        tracked: "sv.Detections",
        ages: list[int],
    ) -> None:
        with self._lock:
            frame_msg = self._latest_frame
        if frame_msg is None:
            return
        arr = np.frombuffer(bytes(frame_msg.data), dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            return

        img_h = bgr.shape[0]
        if tracked.tracker_id is not None:
            for i in range(len(tracked)):
                tid = tracked.tracker_id[i]
                if tid is None:
                    continue
                x1, y1, x2, y2 = tracked.xyxy[i].astype(int)
                color = _color_for_track(int(tid))
                cv2.rectangle(bgr, (x1, y1), (x2, y2), color, 2)
                bbox_h = max(1, y2 - y1)
                est_d = FOCAL_Y_PX * PERSON_HEIGHT_M / bbox_h
                dist_conf = _distance_confidence(y1, y2, img_h)
                age = ages[i] if i < len(ages) else 0
                label = f"#{int(tid)} {est_d:.1f}m a{age} c{dist_conf:.1f}"
                # White-outline text for readability on any background.
                _put_outlined(bgr, label, (x1, max(20, y1 - 8)), color)

        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, OVERLAY_QUALITY])
        if not ok:
            return
        out = CompressedImage()
        out.header = header
        out.format = "jpeg"
        out.data = buf.tobytes()
        self.overlay_pub.publish(out)

    def _publish_health(self) -> None:
        now = time.monotonic()
        with self._lock:
            window = max(1e-3, now - self._window_start)
            count = self._processed_in_window
            self._processed_in_window = 0
            self._window_start = now
            tracks = self._active_tracks
        fps = count / window
        msg = Float32MultiArray()
        dim = MultiArrayDimension()
        dim.label = "fps,active_tracks"
        dim.size = 2
        dim.stride = 2
        msg.layout.dim.append(dim)
        msg.data = [fps, float(tracks)]
        self.health_pub.publish(msg)

    @staticmethod
    def _frame_height(frame_msg: CompressedImage) -> int:
        # Decode just to grab dimensions. Tiny cost vs. the full pipeline.
        try:
            arr = np.frombuffer(bytes(frame_msg.data), dtype=np.uint8)
            bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            return bgr.shape[0] if bgr is not None else 0
        except Exception:
            return 0


def _put_outlined(img, text: str, pos: tuple[int, int], color: tuple[int, int, int]) -> None:
    cv2.putText(img, text, pos, cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, text, pos, cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)


def main():
    rclpy.init()
    if sv is None:
        print("go2_vision_tracker: supervision not available — pip install supervision")
        rclpy.try_shutdown()
        raise SystemExit(1)
    try:
        node = Go2VisionTracker()
    except Exception as exc:
        print(f"go2_vision_tracker: init failed: {exc}")
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
