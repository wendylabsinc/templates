"""Brain-side fusion of UWB decision + vision tracks.

Produces a single `FusedTarget` per tick that the FSM and ghost trail
both consume. The fuser owns the active operator track ID so we don't
oscillate between people when there's a crowd in front of the camera.

Decision tree per tick (high level):

    UWB healthy  +  vision sees the active track
        → "fused" bearing/distance, weighted by confidence
    UWB healthy  +  no vision  / vision lost the track
        → "uwb"   (today's behavior)
    UWB LOST/PREDICTING  +  vision still sees the active track
        → "vision"  (the corner-case fix — round corners visually)
    Both LOST
        → return None  (FSM enters RECOVERING and consults the trail)

We deliberately drop the watchtower-side `vision_agreement: "disagree"
→ vx=0` veto. That heuristic was a one-bit safety hack inside the
filter; the brain-side fuser has more information and rejects bad
bearings as outliers instead, so the dog can still yaw away from a
ghost without freezing forward motion.

Pure data-transformation module — no DDS, no ROS, no I/O. Tests drive it
with scripted Decision/VisionTracks sequences.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .models import Decision, VisionTrack, VisionTracks

logger = logging.getLogger(__name__)


# Tunables. Defaults match the watchtower-side filter so we interpret
# bearings the same way the publisher emits them (camera FOV half-angle,
# bearing-match threshold for ID association). Each is overridable via
# env var and exposed in /params for live tuning (mode_server.py).
FOV_HALF_DEG = float(os.environ.get("BRAIN_FOV_HALF_DEG", "45"))
MATCH_BEARING_DEG = float(os.environ.get("BRAIN_MATCH_BEARING_DEG", "20"))
OUTLIER_GATE_DEG = float(os.environ.get("BRAIN_OUTLIER_GATE_DEG", "30"))
VISION_DECAY_S = float(os.environ.get("BRAIN_VISION_DECAY_S", "0.5"))
TRACK_DROP_AFTER_S = float(os.environ.get("BRAIN_TRACK_DROP_AFTER_S", "3.0"))
FOLLOWABLE_CONFIDENCE = float(
    # Set to PREDICTING's floor (0.4) so the UWB filter can coast through
    # brief sample drops without kicking the FSM out of FOLLOWING. Realistic
    # values: PREDICTING=0.4, ACQUIRING=0.6, TRACKING=0.7..1.0. Below 0.4
    # the signal is genuinely degraded and we want RECOVERING anyway.
    os.environ.get("BRAIN_FOLLOWABLE_CONFIDENCE", "0.4")
)
FOLLOWABLE_MAX_AGE_S = float(
    os.environ.get("BRAIN_FOLLOWABLE_MAX_AGE_S", "0.3")
)
# Tracks younger than this aren't trusted for ID association. BYTETrack
# spits out a fresh ID on every new bbox, including spurious detections.
MIN_TRACK_AGE_FRAMES = int(os.environ.get("BRAIN_MIN_TRACK_AGE_FRAMES", "3"))
# Window used to estimate vision-only target velocity by finite difference
# when UWB doesn't supply closing/lateral rates.
VISION_VEL_WINDOW_S = float(
    os.environ.get("BRAIN_VISION_VEL_WINDOW_S", "1.0")
)


@dataclass
class FusedTarget:
    """Single per-tick perception verdict consumed by the FSM and trail."""

    bearing_deg: float
    distance_m: float
    bearing_source: str           # "uwb" | "vision" | "fused"
    confidence: float             # 0..1, post-fusion
    target_vx_body: float         # +ve = target approaching dog (body-x)
    target_vy_body: float         # +ve = target moving to dog's left (body-y)
    age_s: float                  # newest contributor's age, seconds
    tracking_state: str           # "ACQUIRING" | "TRACKING" | "PREDICTING" | "LOST"
    vision_track_id: Optional[str]
    is_followable: bool           # confidence + age above FSM threshold


class TargetFuser:
    """Stateful fuser. Caches the active operator track ID across ticks
    so brief vision dropouts don't reshuffle the FSM's notion of who the
    operator is."""

    def __init__(self) -> None:
        self._active_id: Optional[str] = None
        self._active_id_seen_at: Optional[float] = None
        # Recent (bearing_deg, distance_m, t_mono) of the active vision
        # track for fallback velocity estimation.
        self._vis_history: List[Tuple[float, float, float]] = []

    @property
    def active_track_id(self) -> Optional[str]:
        return self._active_id

    def reset(self) -> None:
        self._active_id = None
        self._active_id_seen_at = None
        self._vis_history.clear()

    def fuse(
        self,
        decision: Optional[Decision],
        decision_age_s: Optional[float],
        tracks: Optional[VisionTracks],
        vision_age_s: Optional[float],
    ) -> Optional[FusedTarget]:
        """Compute a FusedTarget from this tick's inputs, or None if no
        usable signal exists. `*_age_s` are the wall-clock ages of the
        most recent samples (None when never received)."""
        now = time.monotonic()

        vis_track = self._pick_active_track(decision, tracks)
        if vis_track is not None and vision_age_s is not None:
            self._record_vis_history(vis_track, now)
            self._active_id_seen_at = now
        elif (
            self._active_id_seen_at is not None
            and (now - self._active_id_seen_at) > TRACK_DROP_AFTER_S
        ):
            # Operator's track has been gone long enough that re-acquiring
            # an arbitrary track shouldn't be assumed to be them.
            self._active_id = None
            self._active_id_seen_at = None

        w_uwb = self._uwb_weight(decision)
        w_vis = self._vis_weight(vis_track, vision_age_s)

        if w_uwb == 0.0 and w_vis == 0.0:
            return None

        # Disagreement gate. Drop the lower-weight side as an outlier so
        # the surviving sensor steers cleanly. Both bearings live on the
        # same convention (+ve = left), so a plain difference is fine.
        # The None checks below are invariants enforced by _uwb_weight /
        # _vis_weight (they return 0 if their input is None or unusable).
        # Use explicit raises rather than `assert` so they survive `python -O`.
        if w_uwb > 0 and w_vis > 0:
            if decision is None or vis_track is None or decision.bearing_deg is None:
                raise RuntimeError("fusion invariant: weights>0 imply non-None inputs")
            diff = self._wrap_deg(decision.bearing_deg - vis_track.bearing_deg)
            if abs(diff) > OUTLIER_GATE_DEG:
                if w_uwb >= w_vis:
                    w_vis = 0.0
                    vis_track = None
                else:
                    w_uwb = 0.0

        # Combine.
        if w_uwb > 0 and w_vis > 0:
            if (
                decision is None
                or vis_track is None
                or decision.bearing_deg is None
                or decision.distance_m is None
            ):
                raise RuntimeError("fusion invariant: weights>0 imply non-None inputs")
            tot = w_uwb + w_vis
            bearing_fused = (
                w_uwb * decision.bearing_deg + w_vis * vis_track.bearing_deg
            ) / tot
            distance_fused = (
                w_uwb * decision.distance_m + w_vis * vis_track.distance_m
            ) / tot
            source = "fused"
        elif w_uwb > 0:
            if (
                decision is None
                or decision.bearing_deg is None
                or decision.distance_m is None
            ):
                raise RuntimeError("fusion invariant: w_uwb>0 implies non-None decision")
            bearing_fused = decision.bearing_deg
            distance_fused = decision.distance_m
            source = "uwb"
        else:
            if vis_track is None:
                raise RuntimeError("fusion invariant: w_vis>0 implies non-None vis_track")
            bearing_fused = vis_track.bearing_deg
            distance_fused = vis_track.distance_m
            source = "vision"

        tracking_state = self._derive_tracking_state(decision, w_vis > 0)
        target_vx_body, target_vy_body = self._velocities(
            decision, w_uwb > 0, now
        )

        confidence = max(0.0, min(1.0, max(w_uwb, w_vis)))

        ages: List[float] = []
        if w_uwb > 0 and decision_age_s is not None:
            ages.append(decision_age_s)
        if w_vis > 0 and vision_age_s is not None:
            ages.append(vision_age_s)
        age = min(ages) if ages else 0.0

        is_followable = (
            confidence >= FOLLOWABLE_CONFIDENCE
            and age <= FOLLOWABLE_MAX_AGE_S
            and tracking_state in ("TRACKING", "PREDICTING")
        )

        return FusedTarget(
            bearing_deg=float(bearing_fused),
            distance_m=float(distance_fused),
            bearing_source=source,
            confidence=float(confidence),
            target_vx_body=float(target_vx_body),
            target_vy_body=float(target_vy_body),
            age_s=float(age),
            tracking_state=tracking_state,
            vision_track_id=self._active_id,
            is_followable=bool(is_followable),
        )

    # -- internals ----------------------------------------------------------

    def _pick_active_track(
        self,
        decision: Optional[Decision],
        tracks: Optional[VisionTracks],
    ) -> Optional[VisionTrack]:
        """Identify which of the (possibly many) vision tracks is the
        operator. Cached ID first; UWB-bearing match second; nothing
        otherwise. We never *guess* an active track without UWB
        corroboration — that's the path to following the wrong person.

        Re-association mode: once we've ever had a lock (within
        TRACK_DROP_AFTER_S of last sighting), the MIN_TRACK_AGE_FRAMES
        gate is loosened — a fresh track at the UWB-corroborated bearing
        is almost certainly the operator coming back into view, and
        UWB-bearing match is itself strong evidence. Without this, a
        BYTETrack id flip costs ~MIN/fps seconds of vision-blind even
        though the operator is right in front of the camera."""
        if tracks is None or not tracks.tracks:
            return None

        in_reassoc = self._active_id_seen_at is not None

        if self._active_id is not None:
            for t in tracks.tracks:
                if t.id != self._active_id:
                    continue
                if t.age_frames >= MIN_TRACK_AGE_FRAMES or in_reassoc:
                    return t

        if (
            decision is not None
            and decision.tracking_state in ("TRACKING", "PREDICTING")
            and decision.bearing_deg is not None
            and abs(decision.bearing_deg) <= FOV_HALF_DEG
        ):
            best: Optional[VisionTrack] = None
            best_diff = MATCH_BEARING_DEG + 1.0
            for t in tracks.tracks:
                if t.age_frames < MIN_TRACK_AGE_FRAMES and not in_reassoc:
                    continue
                diff = abs(self._wrap_deg(t.bearing_deg - decision.bearing_deg))
                if diff < best_diff:
                    best = t
                    best_diff = diff
            if best is not None and best_diff <= MATCH_BEARING_DEG:
                if best.id != self._active_id:
                    logger.info(
                        "fusion: active track id %r → %r "
                        "(bearing match %.1f°, reassoc=%s)",
                        self._active_id, best.id, best_diff, in_reassoc,
                    )
                    self._active_id = best.id
                return best

        return None

    def _uwb_weight(self, decision: Optional[Decision]) -> float:
        # ACQUIRING is included so the fuser produces a non-None FusedTarget
        # during UWB's cold-start window. The FSM keys off tracking_state
        # to route ACQUIRING → SEARCHING (not FOLLOWING), so leaking weight
        # here is safe — `is_followable` still excludes ACQUIRING from the
        # FOLLOWING gate downstream.
        if (
            decision is None
            or decision.bearing_deg is None
            or decision.distance_m is None
        ):
            return 0.0
        if decision.tracking_state not in ("ACQUIRING", "TRACKING", "PREDICTING"):
            return 0.0
        return float(decision.confidence or 0.0)

    def _vis_weight(
        self, track: Optional[VisionTrack], vision_age_s: Optional[float]
    ) -> float:
        if track is None or vision_age_s is None:
            return 0.0
        decay = math.exp(-vision_age_s / max(VISION_DECAY_S, 1e-3))
        # Slow ramp-in over the track's first ~10 frames so a flicker
        # detection can't briefly outweigh a healthy UWB reading.
        age_factor = min(1.0, track.age_frames / 10.0)
        return float(track.dist_confidence * decay * age_factor)

    def _derive_tracking_state(
        self, decision: Optional[Decision], have_vision: bool
    ) -> str:
        """Map per-source states into the FSM's existing vocabulary so
        downstream code (state_machine, intent_publisher) doesn't have to
        learn new strings. Vision *promotes* PREDICTING to TRACKING
        because we have a live measurement again, just from a different
        sensor."""
        if decision is None:
            return "TRACKING" if have_vision else "LOST"
        if decision.tracking_state == "ACQUIRING":
            return "ACQUIRING"
        if decision.tracking_state == "TRACKING":
            return "TRACKING"
        if decision.tracking_state == "PREDICTING":
            return "TRACKING" if have_vision else "PREDICTING"
        return "TRACKING" if have_vision else "LOST"

    def _record_vis_history(self, t: VisionTrack, now: float) -> None:
        self._vis_history.append((t.bearing_deg, t.distance_m, now))
        cutoff = now - VISION_VEL_WINDOW_S
        while self._vis_history and self._vis_history[0][2] < cutoff:
            self._vis_history.pop(0)

    def _velocities(
        self,
        decision: Optional[Decision],
        have_uwb: bool,
        now: float,
    ) -> Tuple[float, float]:
        """Return (target_vx_body, target_vy_body). Prefer UWB Kalman
        rates when present, otherwise finite-difference the recent vision
        history. Either may be (0, 0) when too little data exists yet."""
        if (
            have_uwb
            and decision is not None
            and decision.closing_rate_mps is not None
            and decision.lateral_rate_mps is not None
        ):
            return (
                float(decision.closing_rate_mps),
                float(decision.lateral_rate_mps),
            )

        if len(self._vis_history) < 2:
            return 0.0, 0.0
        b0, d0, t0 = self._vis_history[0]
        b1, d1, t1 = self._vis_history[-1]
        dt = max(1e-3, t1 - t0)
        x0 = d0 * math.cos(math.radians(b0))
        y0 = d0 * math.sin(math.radians(b0))
        x1 = d1 * math.cos(math.radians(b1))
        y1 = d1 * math.sin(math.radians(b1))
        # closing_rate is +ve when target approaches the dog. Body-x
        # decreases as the target approaches → closing = -(dx)/dt.
        target_vx_body = (x0 - x1) / dt
        # lateral_rate is +ve when target moves to dog's left (body-y up).
        target_vy_body = (y1 - y0) / dt
        return float(target_vx_body), float(target_vy_body)

    @staticmethod
    def _wrap_deg(deg: float) -> float:
        """Wrap to [-180, 180]. Cheap and correct for our use (small angles)."""
        while deg > 180.0:
            deg -= 360.0
        while deg < -180.0:
            deg += 360.0
        return deg
