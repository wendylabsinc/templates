#!/usr/bin/env python3
"""
go2_uwb_filter.py

Phase 2 of the UWB subsystem. Subscribes to `/go2/uwb/point` (the
unfiltered Phase-1 stream in base_link) and runs a 2D constant-velocity
Kalman filter with state = (x, y, vx, vy). Publishes:

    /go2/uwb/target    nav_msgs/Odometry    filtered pose + twist + cov
    /go2/uwb/state     std_msgs/String      ACQUIRING | TRACKING | PREDICTING | LOST
    /go2/uwb/decision  std_msgs/String      JSON struct for brain consumers
    /go2/uwb/path      nav_msgs/Path        last N seconds of filtered positions

The decision topic is the brain-facing contract — it consolidates everything
a follower controller needs into one JSON payload so the brain doesn't have
to re-derive geometry on every tick:

    {
      "stamp_ns":              int monotonic nanoseconds (clock-aligned with /go2/uwb/state)
      "tracking_state":        "LOST" | "ACQUIRING" | "TRACKING" | "PREDICTING"
      "distance_m":            float | null                 — None when LOST
      "bearing_deg":           float | null                 — +ve = left
      "closing_rate_mps":      float | null                 — +ve = approaching the dog
      "lateral_rate_mps":      float | null                 — +ve = moving to the dog's left
      "sector":                "ahead" | "left" | "right" | "behind" | "lost"
      "follow_distance_status":"too_close" | "ok" | "too_far" | "lost"
      "confidence":            float in [0, 1]              — combines tracking_state + age
    }

State machine:
    LOST       no usable estimate; awaits a fresh measurement.
    ACQUIRING  first ~1 s after re-acquisition; estimate is published
               but the consumer should treat it as low-confidence.
    TRACKING   measurements arriving; filter is healthy.
    PREDICTING no measurement in the last PREDICT_TIMEOUT_S; filter
               keeps predicting forward, but estimate is degrading.
               Demoted to LOST after PREDICT_TIMEOUT_S (the spec says
               we don't predict beyond 500 ms).

Outlier rejection: each measurement is gated by Mahalanobis distance
against the predicted innovation covariance. The default gate is the
χ² 99 % threshold for 2 degrees of freedom (≈ 9.21).

All tuning knobs are env-var overridable; Phase 3 promotes them to a
YAML config file.
"""

import json
import math
import os
import time
from collections import deque
from threading import Lock

import numpy as np
import rclpy
from geometry_msgs.msg import PointStamped, PoseStamped
from nav_msgs.msg import Odometry, Path
from rclpy.node import Node
from std_msgs.msg import String
from vision_msgs.msg import Detection2DArray


# --- Tunables ---------------------------------------------------------------
# Process noise (continuous white-noise acceleration model).
PROCESS_NOISE_POS = float(os.environ.get("UWB_Q_POS", "0.01"))      # m^2 — small slack on top of the CV model
PROCESS_NOISE_VEL = float(os.environ.get("UWB_Q_VEL", "0.5"))       # (m/s^2)^2 — assumed accel variance
# Measurement noise (range/bearing → x/y simplified to fixed isotropic).
MEAS_NOISE_XY = float(os.environ.get("UWB_R_XY", "0.09"))           # m^2  (≈30 cm RMS at 1–5 m, per spec)
# Outlier gate: χ² 99 % for 2 DOF.
MAHAL_GATE = float(os.environ.get("UWB_MAHAL_GATE", "9.21"))
# State-machine timing.
ACQUIRING_S = float(os.environ.get("UWB_ACQUIRING_S", "1.0"))
PREDICT_TIMEOUT_S = float(os.environ.get("UWB_PREDICT_TIMEOUT_S", "0.5"))
# Output rate.
PUBLISH_HZ = float(os.environ.get("UWB_PUBLISH_HZ", "20"))
# Diagnostic logging cadence. The heartbeat surfaces state + recent
# accept/reject totals at HEARTBEAT_PERIOD_S; outlier rejection logs
# are throttled so a multipath storm doesn't flood `wendy logs`
# (the totals still show up in the heartbeat).
HEARTBEAT_PERIOD_S = float(os.environ.get("UWB_FILTER_HEARTBEAT_S", "1.0"))
REJECT_LOG_THROTTLE_S = float(os.environ.get("UWB_FILTER_REJECT_LOG_S", "1.0"))
# Decision-helper tunables (consumed by brain code via /go2/uwb/decision).
FOLLOW_MIN_M = float(os.environ.get("UWB_FOLLOW_MIN_M", "0.3"))
FOLLOW_MAX_M = float(os.environ.get("UWB_FOLLOW_MAX_M", "0.6"))
AHEAD_HALF_DEG = float(os.environ.get("UWB_AHEAD_HALF_DEG", "15"))
BEHIND_HALF_DEG = float(os.environ.get("UWB_BEHIND_HALF_DEG", "90"))
PATH_HISTORY_S = float(os.environ.get("UWB_PATH_HISTORY_S", "5.0"))
# Vision-fusion knobs. We subscribe to /go2/vision/tracked_persons and use
# the bbox centre to derive each track's bearing in base_link (assuming a
# forward-facing camera, principal point at image centre, square pixels).
# When at least one track's bearing matches the UWB bearing within
# AGREE_DEG, we BOOST confidence (vision agrees with UWB → very likely the
# right person). When the UWB bearing falls inside the camera's FOV but no
# track matches, we DEMOTE confidence (could be a stale tag, decoy, or a
# person we should be following but vision missed). Outside the camera FOV
# vision can't see anyway, so we leave confidence untouched.
VISION_FY = float(os.environ.get("VISION_FY", "615"))                # px (placeholder)
VISION_IMAGE_WIDTH = int(os.environ.get("VISION_IMAGE_WIDTH", "1280"))
VISION_FOV_HALF_DEG = float(os.environ.get("VISION_FOV_HALF_DEG", "45"))
VISION_AGREE_BEARING_DEG = float(os.environ.get("UWB_VISION_AGREE_DEG", "20"))
VISION_AGREE_FLOOR = float(os.environ.get("UWB_VISION_AGREE_FLOOR", "0.95"))
VISION_DISAGREE_PENALTY = float(os.environ.get("UWB_VISION_DISAGREE_PENALTY", "0.7"))
VISION_TRACK_STALE_S = float(os.environ.get("UWB_VISION_TRACK_STALE_S", "0.5"))
# Beyond this distance, YOLO11n on the Go2's front camera stops
# resolving people reliably (bboxes get tiny, confidence drops).
# Reporting `disagree` past this range would be a sensor-limit lie:
# vision isn't contradicting UWB, it just can't see that far. We
# return `out_of_range` instead, which downstream consumers (brain)
# treat the same as `no_vision` — no confidence boost, no penalty.
VISION_MAX_RANGE_M = float(os.environ.get("UWB_VISION_MAX_RANGE_M", "8.0"))
# ---------------------------------------------------------------------------


class TrackingState:
    LOST = "LOST"
    ACQUIRING = "ACQUIRING"
    TRACKING = "TRACKING"
    PREDICTING = "PREDICTING"


class KalmanFilter2D:
    """Constant-velocity 2D KF. State = [x, y, vx, vy]^T."""

    def __init__(self, q_pos: float, q_vel: float, r_xy: float):
        self.q_pos = q_pos
        self.q_vel = q_vel
        self.R = np.eye(2) * r_xy
        self.H = np.array([[1.0, 0.0, 0.0, 0.0],
                           [0.0, 1.0, 0.0, 0.0]])
        self.x = np.zeros((4, 1))
        self.P = np.eye(4)
        self.initialized = False

    def initialize(self, x: float, y: float) -> None:
        self.x = np.array([[x], [y], [0.0], [0.0]])
        # Position uncertainty matches the measurement noise; velocity unknown
        # → wide initial variance so the first few updates can pull it in.
        self.P = np.diag([self.R[0, 0], self.R[1, 1], 4.0, 4.0])
        self.initialized = True

    def predict(self, dt: float) -> None:
        F = np.array([
            [1.0, 0.0, dt,  0.0],
            [0.0, 1.0, 0.0, dt ],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ])
        # Discrete white-noise acceleration Q (Bar-Shalom et al.).
        q_v = self.q_vel
        Q = np.array([
            [dt**4 / 4 * q_v, 0.0,             dt**3 / 2 * q_v, 0.0],
            [0.0,             dt**4 / 4 * q_v, 0.0,             dt**3 / 2 * q_v],
            [dt**3 / 2 * q_v, 0.0,             dt**2 * q_v,     0.0],
            [0.0,             dt**3 / 2 * q_v, 0.0,             dt**2 * q_v],
        ])
        # Small additive position-process noise to absorb model mis-spec.
        Q[0, 0] += self.q_pos
        Q[1, 1] += self.q_pos
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q

    def update(self, z: np.ndarray) -> tuple[float, bool]:
        """Run an update step. Returns (mahalanobis_distance_sq, accepted)."""
        innovation = z - self.H @ self.x
        S = self.H @ self.P @ self.H.T + self.R
        try:
            S_inv = np.linalg.inv(S)
        except np.linalg.LinAlgError:
            return float("inf"), False
        d2 = float((innovation.T @ S_inv @ innovation)[0, 0])
        if d2 > MAHAL_GATE:
            return d2, False
        K = self.P @ self.H.T @ S_inv
        self.x = self.x + K @ innovation
        self.P = (np.eye(4) - K @ self.H) @ self.P
        return d2, True


class Go2UwbFilter(Node):
    def __init__(self) -> None:
        super().__init__("go2_uwb_filter")

        self.target_pub = self.create_publisher(Odometry, "/go2/uwb/target", 10)
        self.state_pub = self.create_publisher(String, "/go2/uwb/state", 10)
        self.decision_pub = self.create_publisher(String, "/go2/uwb/decision", 10)
        self.path_pub = self.create_publisher(Path, "/go2/uwb/path", 10)

        self.create_subscription(PointStamped, "/go2/uwb/point", self._on_point, 10)
        self.create_subscription(
            Detection2DArray, "/go2/vision/tracked_persons", self._on_tracks, 10
        )
        self.create_timer(1.0 / PUBLISH_HZ, self._tick)

        self._kf = KalmanFilter2D(PROCESS_NOISE_POS, PROCESS_NOISE_VEL, MEAS_NOISE_XY)
        self._lock = Lock()
        self._state = TrackingState.LOST
        self._acq_start: float | None = None
        self._tracking_since: float | None = None  # set when state enters TRACKING
        self._last_meas: float | None = None
        self._last_predict: float | None = None
        self._pending: tuple[float, float] | None = None
        self._accepted_count = 0
        self._rejected_count = 0
        # Diagnostic state. `_first_point_logged` fires once per node
        # lifetime so the supervisor's log shows the bridge → filter
        # link came up. `_last_*_at_hb` lets the 1 Hz heartbeat report
        # accept/reject *deltas* not totals. `_last_reject_log` is the
        # throttle for outlier-rejection warnings.
        self._first_point_logged = False
        self._last_heartbeat: float = 0.0
        self._last_accepted_at_hb = 0
        self._last_rejected_at_hb = 0
        self._last_reject_log: float = 0.0
        # Vision-track snapshot: list of (bearing_deg, distance_m_or_None) plus
        # arrival time. Read from the tick thread; written from the vision
        # subscriber callback. Lock guards both.
        self._vision_tracks: list[tuple[float, float | None]] = []
        self._vision_tracks_at: float | None = None
        # Rolling history of filtered positions for /go2/uwb/path. Length sized
        # to PATH_HISTORY_S at the publish rate so the trail walks forward.
        self._path_history: deque[PoseStamped] = deque(
            maxlen=max(2, int(PATH_HISTORY_S * PUBLISH_HZ))
        )

        self.get_logger().info(
            f"UWB filter started: q_pos={PROCESS_NOISE_POS}, q_vel={PROCESS_NOISE_VEL}, "
            f"r_xy={MEAS_NOISE_XY}, gate={MAHAL_GATE}, "
            f"acquiring={ACQUIRING_S}s, predict_timeout={PREDICT_TIMEOUT_S}s, "
            f"publish={PUBLISH_HZ} Hz"
        )

    def _on_point(self, msg: PointStamped) -> None:
        with self._lock:
            self._pending = (float(msg.point.x), float(msg.point.y))
            first = not self._first_point_logged
            self._first_point_logged = True
        if first:
            self.get_logger().info(
                f"first /go2/uwb/point received: "
                f"({float(msg.point.x):.2f}, {float(msg.point.y):.2f}) m"
            )

    def _on_tracks(self, msg: Detection2DArray) -> None:
        # Snap the latest tracker output. Each detection's bbox centre maps
        # to a bearing in base_link via a pinhole approximation; distance
        # comes from the side-channel scalar in the result hypothesis (z =
        # est_distance_m). We store only the geometry — track IDs aren't
        # needed for agreement gating.
        cx_image = VISION_IMAGE_WIDTH / 2.0
        tracks: list[tuple[float, float | None]] = []
        for det in msg.detections:
            cx_pixel = float(det.bbox.center.position.x)
            # +ve bearing = left in REP-103; pixel x increases to the right,
            # so a pixel left of centre yields a positive bearing.
            bearing_rad = math.atan2(cx_image - cx_pixel, VISION_FY)
            bearing_deg = math.degrees(bearing_rad)
            distance_m: float | None = None
            if det.results:
                z = float(det.results[0].pose.pose.position.z)
                if z > 0:
                    distance_m = z
            tracks.append((bearing_deg, distance_m))
        with self._lock:
            self._vision_tracks = tracks
            self._vision_tracks_at = time.monotonic()

    def _tick(self) -> None:
        now = time.monotonic()
        with self._lock:
            pending = self._pending
            self._pending = None

            # Predict step (only if filter is initialized).
            if self._kf.initialized and self._last_predict is not None:
                dt = max(1e-3, min(1.0, now - self._last_predict))
                self._kf.predict(dt)
            self._last_predict = now

            if pending is not None:
                x, y = pending
                if not self._kf.initialized:
                    # First measurement (or first after a LOST).
                    self._kf.initialize(x, y)
                    self._set_state(
                        TrackingState.ACQUIRING,
                        f"first measurement at ({x:.2f}, {y:.2f}) m",
                    )
                    self._acq_start = now
                    self._last_meas = now
                else:
                    z = np.array([[x], [y]])
                    _d2, accepted = self._kf.update(z)
                    if accepted:
                        self._accepted_count += 1
                        self._last_meas = now
                        prev_state = self._state
                        if self._state == TrackingState.PREDICTING:
                            self._set_state(
                                TrackingState.TRACKING,
                                "accepted update; resuming from PREDICTING",
                            )
                        elif self._state == TrackingState.ACQUIRING:
                            if self._acq_start is not None and (now - self._acq_start) >= ACQUIRING_S:
                                self._set_state(
                                    TrackingState.TRACKING,
                                    f"acquired ({ACQUIRING_S:.1f}s of accepted updates)",
                                )
                        if self._state == TrackingState.TRACKING and prev_state != TrackingState.TRACKING:
                            self._tracking_since = now
                    else:
                        self._rejected_count += 1
                        # Rate-limit so a multipath burst doesn't drown
                        # the log; running totals are in the heartbeat.
                        if (now - self._last_reject_log) >= REJECT_LOG_THROTTLE_S:
                            self.get_logger().warn(
                                f"outlier rejected: mahal²={_d2:.2f} > gate={MAHAL_GATE:.2f}, "
                                f"z=({x:.2f},{y:.2f}) m, state={self._state}"
                            )
                            self._last_reject_log = now
                        # Reject the sample but don't change state — a single
                        # multipath spike shouldn't demote TRACKING.
            else:
                # No measurement this tick.
                if self._kf.initialized and self._last_meas is not None:
                    age = now - self._last_meas
                    if age > PREDICT_TIMEOUT_S:
                        # Spec: don't predict past 500 ms — declare LOST and
                        # discard the filter so the next sample re-acquires.
                        self._set_state(
                            TrackingState.LOST,
                            f"no measurement for {age * 1000:.0f} ms",
                        )
                        self._kf.initialized = False
                        self._tracking_since = None
                        self._path_history.clear()
                    elif self._state == TrackingState.TRACKING:
                        self._set_state(
                            TrackingState.PREDICTING,
                            "no measurement this tick; coasting",
                        )

            cur_state = self._state
            kf_init = self._kf.initialized
            x_state = self._kf.x.copy() if kf_init else None
            P_state = self._kf.P.copy() if kf_init else None

        # Publish outside the lock so a slow subscriber can't stall the tick.
        self._publish_state(cur_state)
        if kf_init and x_state is not None and P_state is not None:
            self._publish_target(x_state, P_state, cur_state)
            self._publish_path(x_state)
        self._publish_decision(cur_state, x_state, now)

        # 1 Hz stdout heartbeat — current verdict + accept/reject deltas
        # so a stuttering filter is obvious from `wendy logs` alone.
        if (now - self._last_heartbeat) >= HEARTBEAT_PERIOD_S:
            with self._lock:
                accepted_total = self._accepted_count
                rejected_total = self._rejected_count
                tracks_at = self._vision_tracks_at
            self._log_heartbeat(
                cur_state, x_state, now,
                accepted_total, rejected_total, tracks_at,
            )
            self._last_heartbeat = now

    def _set_state(self, new_state: str, reason: str) -> None:
        """Internal: transition to `new_state` and log it. No-op if same."""
        if self._state == new_state:
            return
        prev = self._state
        self._state = new_state
        self.get_logger().info(f"state {prev} → {new_state} ({reason})")

    def _log_heartbeat(
        self,
        state: str,
        x: np.ndarray | None,
        now: float,
        accepted_total: int,
        rejected_total: int,
        tracks_at: float | None,
    ) -> None:
        accepted_in = accepted_total - self._last_accepted_at_hb
        rejected_in = rejected_total - self._last_rejected_at_hb
        self._last_accepted_at_hb = accepted_total
        self._last_rejected_at_hb = rejected_total

        if x is None:
            self.get_logger().info(
                f"hb: state={state}, no estimate, "
                f"updates this s = {accepted_in} accepted / {rejected_in} rejected"
            )
            return

        px, py = float(x[0, 0]), float(x[1, 0])
        vx, vy = float(x[2, 0]), float(x[3, 0])
        distance = math.hypot(px, py)
        bearing_deg = math.degrees(math.atan2(py, px))
        speed = math.hypot(vx, vy)
        confidence = self._confidence_for(state, now)
        if tracks_at is None:
            vision_str = "vision=none"
        else:
            vision_str = f"vision_age={(now - tracks_at) * 1000:.0f}ms"
        self.get_logger().info(
            f"hb: state={state}, "
            f"range={distance:.2f} m, bearing={bearing_deg:+.1f}°, "
            f"speed={speed:.2f} m/s ({vx:+.2f},{vy:+.2f}), "
            f"conf={confidence:.2f}, "
            f"updates this s = {accepted_in} accepted / {rejected_in} rejected, "
            f"{vision_str}"
        )

    def _publish_state(self, state: str) -> None:
        msg = String()
        msg.data = state
        self.state_pub.publish(msg)

    def _publish_target(self, x: np.ndarray, P: np.ndarray, state: str) -> None:
        msg = Odometry()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = "base_link"
        msg.child_frame_id = "uwb_tag"

        px, py = float(x[0, 0]), float(x[1, 0])
        vx, vy = float(x[2, 0]), float(x[3, 0])

        msg.pose.pose.position.x = px
        msg.pose.pose.position.y = py
        msg.pose.pose.position.z = 0.0
        # Orientation isn't tracked; use yaw derived from velocity heading
        # so downstream consumers get a sensible "facing" direction.
        if math.hypot(vx, vy) > 0.05:
            yaw = math.atan2(vy, vx)
        else:
            yaw = math.atan2(py, px)
        msg.pose.pose.orientation.z = math.sin(yaw / 2.0)
        msg.pose.pose.orientation.w = math.cos(yaw / 2.0)

        # Pose covariance is a 6×6 row-major flatten over (x,y,z,rx,ry,rz).
        # We have (x,y) — fill the 2×2 block at positions 0,1,6,7.
        pose_cov = [0.0] * 36
        pose_cov[0] = float(P[0, 0])
        pose_cov[1] = float(P[0, 1])
        pose_cov[6] = float(P[1, 0])
        pose_cov[7] = float(P[1, 1])
        msg.pose.covariance = pose_cov

        msg.twist.twist.linear.x = vx
        msg.twist.twist.linear.y = vy
        twist_cov = [0.0] * 36
        twist_cov[0] = float(P[2, 2])
        twist_cov[1] = float(P[2, 3])
        twist_cov[6] = float(P[3, 2])
        twist_cov[7] = float(P[3, 3])
        msg.twist.covariance = twist_cov

        self.target_pub.publish(msg)

    def _publish_path(self, x: np.ndarray) -> None:
        pose = PoseStamped()
        pose.header.stamp = self.get_clock().now().to_msg()
        pose.header.frame_id = "base_link"
        pose.pose.position.x = float(x[0, 0])
        pose.pose.position.y = float(x[1, 0])
        pose.pose.position.z = 0.0
        pose.pose.orientation.w = 1.0
        self._path_history.append(pose)

        path = Path()
        path.header.stamp = pose.header.stamp
        path.header.frame_id = "base_link"
        path.poses = list(self._path_history)
        self.path_pub.publish(path)

    def _check_vision_agreement(
        self, uwb_bearing_deg: float, distance_m: float, now_mono: float
    ) -> str:
        """Return one of:

        agree         at least one tracked person matches UWB within AGREE_DEG.
        disagree      UWB inside FOV + range but no track matches.
        out_of_view   UWB bearing outside camera FOV — vision can't see.
        out_of_range  UWB target farther than vision can reliably resolve.
        no_vision     no recent track samples (stale or never received).

        Order of checks matters: range first (sensor limit), then FOV
        (geometric), then track matching. `out_of_range` is the honest
        verdict when YOLO can't be expected to see the target — if we
        instead reported `disagree`, the brain would clamp vx and the
        dog would refuse to walk toward someone 8 m away.
        """
        with self._lock:
            tracks = list(self._vision_tracks)
            tracks_at = self._vision_tracks_at
        if tracks_at is None or (now_mono - tracks_at) > VISION_TRACK_STALE_S:
            return "no_vision"
        if distance_m > VISION_MAX_RANGE_M:
            return "out_of_range"
        if abs(uwb_bearing_deg) > VISION_FOV_HALF_DEG:
            return "out_of_view"
        for vbearing, _ in tracks:
            if abs(vbearing - uwb_bearing_deg) <= VISION_AGREE_BEARING_DEG:
                return "agree"
        return "disagree"

    def _publish_decision(
        self,
        state: str,
        x: np.ndarray | None,
        now_mono: float,
    ) -> None:
        """Pack the brain-facing decision JSON. See module docstring for schema."""
        stamp_ns = int(now_mono * 1_000_000_000)
        if state == TrackingState.LOST or x is None:
            payload = {
                "stamp_ns": stamp_ns,
                "tracking_state": state,
                "distance_m": None,
                "bearing_deg": None,
                "closing_rate_mps": None,
                "lateral_rate_mps": None,
                "sector": "lost",
                "follow_distance_status": "lost",
                "confidence": 0.0,
                "vision_agreement": "no_vision",
            }
        else:
            px, py = float(x[0, 0]), float(x[1, 0])
            vx, vy = float(x[2, 0]), float(x[3, 0])
            distance = math.hypot(px, py)
            bearing_deg = math.degrees(math.atan2(py, px))
            if distance > 1e-6:
                # +closing_rate when target's velocity vector points back toward
                # the dog (i.e. radial distance is shrinking).
                closing_rate = -(vx * px + vy * py) / distance
                # Tangential component, +ve when target moves CCW around dog =
                # to the dog's left in REP-103.
                lateral_rate = (-py * vx + px * vy) / distance
            else:
                closing_rate = 0.0
                lateral_rate = 0.0

            abs_b = abs(bearing_deg)
            if abs_b <= AHEAD_HALF_DEG:
                sector = "ahead"
            elif abs_b > BEHIND_HALF_DEG:
                sector = "behind"
            elif bearing_deg > 0:
                sector = "left"
            else:
                sector = "right"

            if distance < FOLLOW_MIN_M:
                follow_status = "too_close"
            elif distance > FOLLOW_MAX_M:
                follow_status = "too_far"
            else:
                follow_status = "ok"

            base_conf = self._confidence_for(state, now_mono)
            agreement = self._check_vision_agreement(bearing_deg, distance, now_mono)
            if agreement == "agree":
                # Vision corroborates UWB — snap confidence to the floor (or
                # higher if the base was already higher).
                fused_conf = max(base_conf, VISION_AGREE_FLOOR)
            elif agreement == "disagree":
                # UWB says target is here, vision sees no one. Could be a
                # decoy, a multipath ghost, or tag-on-wrong-person. Penalise.
                fused_conf = base_conf * VISION_DISAGREE_PENALTY
            else:
                # out_of_view or no_vision — leave UWB alone.
                fused_conf = base_conf

            payload = {
                "stamp_ns": stamp_ns,
                "tracking_state": state,
                "distance_m": round(distance, 3),
                "bearing_deg": round(bearing_deg, 2),
                "closing_rate_mps": round(closing_rate, 3),
                "lateral_rate_mps": round(lateral_rate, 3),
                "sector": sector,
                "follow_distance_status": follow_status,
                "confidence": round(min(1.0, max(0.0, fused_conf)), 3),
                "vision_agreement": agreement,
            }

        msg = String()
        msg.data = json.dumps(payload)
        self.decision_pub.publish(msg)

    def _confidence_for(self, state: str, now_mono: float) -> float:
        """0..1 score combining tracking_state with how long we've been TRACKING.

        - LOST = 0.0 (no estimate)
        - PREDICTING = 0.4 (estimate is coasting; degrading fast)
        - ACQUIRING = 0.6 (filter has a fresh measurement but hasn't earned trust)
        - TRACKING = ramps from 0.7 → 1.0 over the first 6 s of stable tracking
        """
        if state == TrackingState.LOST:
            return 0.0
        if state == TrackingState.PREDICTING:
            return 0.4
        if state == TrackingState.ACQUIRING:
            return 0.6
        # TRACKING
        if self._tracking_since is None:
            return 0.7
        age = now_mono - self._tracking_since
        return min(1.0, 0.7 + age * 0.05)


def main():
    rclpy.init()
    try:
        node = Go2UwbFilter()
    except Exception as e:
        print(f"go2_uwb_filter: init failed: {e}")
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
