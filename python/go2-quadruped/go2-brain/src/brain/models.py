"""Pure-Python dataclasses for the brain's perception inputs.

Kept separate from `perception.py` so tests + tooling can import the
schemas without dragging in CycloneDDS. The on-wire format and the
publisher contracts live here; the DDS readers in `perception.py` only
add the threading + take-loop machinery on top.

Topic ↔ dataclass map (all `std_msgs/String` with JSON in `data`):

    rt/go2/uwb/decision                → Decision
    rt/go2/vision/tracked_persons_json → VisionTracks (with VisionTrack)
    rt/go2/dog/pose_json               → Pose
    rt/go2/perception/free_space       → FreeSpace
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Decision:
    """Parsed view of /go2/uwb/decision. Mirrors the schema documented
    in go2-Watchtower/go2_uwb_filter.py."""

    tracking_state: str = "LOST"        # LOST | ACQUIRING | TRACKING | PREDICTING
    distance_m: Optional[float] = None
    bearing_deg: Optional[float] = None
    closing_rate_mps: Optional[float] = None
    lateral_rate_mps: Optional[float] = None
    sector: str = "lost"                # ahead | left | right | behind | lost
    follow_distance_status: str = "lost"  # too_close | ok | too_far | lost
    confidence: float = 0.0
    # Vision-fusion verdict from watchtower's UWB filter:
    #   agree         vision sees a person matching the UWB bearing
    #   disagree      UWB direction is inside camera FOV + range but no
    #                 person matches → likely decoy/multipath. Was used
    #                 by the FSM to clamp vx; superseded by brain-side
    #                 fuser disagreement gate.
    #   out_of_view   UWB direction outside camera FOV (geometric miss)
    #   out_of_range  UWB target farther than vision can reliably resolve
    #   no_vision     no recent vision tracks (stale or never received)
    vision_agreement: str = "no_vision"
    stamp_ns: int = 0

    @classmethod
    def from_json(cls, raw: str) -> "Decision":
        try:
            payload = json.loads(raw)
            return cls(
                tracking_state=str(payload.get("tracking_state", "LOST")),
                distance_m=payload.get("distance_m"),
                bearing_deg=payload.get("bearing_deg"),
                closing_rate_mps=payload.get("closing_rate_mps"),
                lateral_rate_mps=payload.get("lateral_rate_mps"),
                sector=str(payload.get("sector", "lost")),
                follow_distance_status=str(
                    payload.get("follow_distance_status", "lost")
                ),
                confidence=float(payload.get("confidence", 0.0) or 0.0),
                vision_agreement=str(
                    payload.get("vision_agreement", "no_vision")
                ),
                stamp_ns=int(payload.get("stamp_ns", 0) or 0),
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            # Don't take down the subscriber thread on a malformed payload
            # or schema drift — log and emit a defaulted "LOST" Decision
            # so the FSM keeps running. If you see this in logs, watchtower
            # and brain disagree on the schema.
            logger.warning("decision parse failed (%s): %r", exc, raw[:200])
            return cls()


@dataclass
class VisionTrack:
    """One person track from /go2/vision/tracked_persons_json. Fields
    mirror the JSON publisher in go2-Watchtower/go2_vision_tracker.py.
    `bearing_deg` follows REP-103 (+ve = left); `distance_m` is bbox-
    height-derived, scaled by VISION_FY (placeholder until calibration).
    Use `dist_confidence` (0.3..1.0) to weight distance fusion — drops
    to ~0.3 when the bbox touches the frame edge."""

    id: str = ""
    bearing_deg: float = 0.0
    distance_m: float = 0.0
    dist_confidence: float = 0.0
    age_frames: int = 0
    score: float = 0.0
    bbox_h_px: float = 0.0


@dataclass
class VisionTracks:
    """Snapshot of every active person track in one inference tick."""

    stamp_ns: int = 0
    image_age_ms: int = 0
    tracks: List[VisionTrack] = field(default_factory=list)

    @classmethod
    def from_json(cls, raw: str) -> "VisionTracks":
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("vision tracks JSON decode failed: %s", exc)
            return cls()
        tracks: List[VisionTrack] = []
        for raw_track in payload.get("tracks", []) or []:
            try:
                tracks.append(
                    VisionTrack(
                        id=str(raw_track.get("id", "")),
                        bearing_deg=float(raw_track.get("bearing_deg", 0.0)),
                        distance_m=float(raw_track.get("distance_m", 0.0)),
                        dist_confidence=float(
                            raw_track.get("dist_confidence", 0.0)
                        ),
                        age_frames=int(raw_track.get("age_frames", 0) or 0),
                        score=float(raw_track.get("score", 0.0)),
                        bbox_h_px=float(raw_track.get("bbox_h_px", 0.0)),
                    )
                )
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "vision track entry malformed (%s): %r", exc, raw_track
                )
        return cls(
            stamp_ns=int(payload.get("stamp_ns", 0) or 0),
            image_age_ms=int(payload.get("image_age_ms", 0) or 0),
            tracks=tracks,
        )


@dataclass
class Pose:
    """World-frame body pose, as published on /go2/dog/pose_json.

    `x_m`/`y_m` are the dog's translation in the world frame chosen by
    sportmodestate (origin set wherever the dog booted up; it doesn't
    matter as long as ghost-trail anchoring uses the same origin
    consistently). `yaw_rad` is +ve = CCW (left)."""

    stamp_ns: int = 0
    x_m: float = 0.0
    y_m: float = 0.0
    yaw_rad: float = 0.0

    @classmethod
    def from_json(cls, raw: str) -> "Pose":
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("pose JSON decode failed: %s", exc)
            return cls()
        return cls(
            stamp_ns=int(payload.get("stamp_ns", 0) or 0),
            x_m=float(payload.get("x_m", 0.0) or 0.0),
            y_m=float(payload.get("y_m", 0.0) or 0.0),
            yaw_rad=float(payload.get("yaw_rad", 0.0) or 0.0),
        )


@dataclass
class FreeSpace:
    """Polar obstacle map derived from the dog's LIDAR by watchtower's
    `go2_lidar_filter.py`. Each entry of `distances_m` is the closest
    return inside one angular sector around the dog (body frame, REP-103:
    +x forward, +y left, +bearing CCW). `inf` (or a sentinel like
    max_range_m) means "no return" — i.e., the sector is clear.

    The whole point is that the brain doesn't need a 3D point cloud —
    it just needs `min_distance_in_cone(bearing, half_angle)` for the
    safety wrapper to decide whether walking forward is safe. The
    contract is intentionally tiny so brain ↔ watchtower stays cheap.

    Schema (one JSON object per scan):
        stamp_ns       int     publisher's monotonic-ish timestamp
        sector_deg     float   angular width of one sector, e.g. 10.0
        max_range_m    float   distances at or above this = "clear"
        distances_m    [float] N entries, sector i covers bearings
                               [-180 + i*sector_deg, -180 + (i+1)*sector_deg)
                               (or 0..360, see `bearing_origin`)
        bearing_origin str     "centered" → first sector starts at -180°
                               "forward"  → first sector starts at -sector_deg/2
                                             (so sector 0 straddles 0° = ahead)

    `bearing_origin` is just a documentation hint for clients that want
    to render the polar plot — `min_distance_in_cone` handles either
    form transparently."""

    stamp_ns: int = 0
    sector_deg: float = 10.0
    max_range_m: float = 5.0
    distances_m: List[float] = field(default_factory=list)
    bearing_origin: str = "centered"

    @classmethod
    def from_json(cls, raw: str) -> "FreeSpace":
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("free_space JSON decode failed: %s", exc)
            return cls()
        try:
            distances = [
                # Sentinel: JSON nulls or non-numeric → treat as max_range.
                float(d) if d is not None else float("inf")
                for d in (payload.get("distances_m") or [])
            ]
        except (TypeError, ValueError) as exc:
            logger.warning("free_space distances_m malformed: %s", exc)
            distances = []
        return cls(
            stamp_ns=int(payload.get("stamp_ns", 0) or 0),
            sector_deg=float(payload.get("sector_deg", 10.0) or 10.0),
            max_range_m=float(payload.get("max_range_m", 5.0) or 5.0),
            distances_m=distances,
            bearing_origin=str(payload.get("bearing_origin", "centered")),
        )

    def min_distance_in_cone(self, bearing_deg: float, half_angle_deg: float) -> float:
        """Smallest obstacle distance inside ±`half_angle_deg` of `bearing_deg`.

        Returns `max_range_m` when no sector intersects the cone — that's
        the "no obstacle inside our reach" reading the safety wrapper
        treats as "go ahead". The wrap-around at ±180° is handled by
        normalizing the centered angle of each sector to [-180, 180]."""
        if not self.distances_m:
            return self.max_range_m
        n = len(self.distances_m)
        sec = self.sector_deg
        if sec <= 0:
            return self.max_range_m

        # Sector i covers [start + i*sec, start + (i+1)*sec). Centered:
        # start at -180. Forward-aligned: start at -sec/2.
        start = -sec / 2.0 if self.bearing_origin == "forward" else -180.0

        worst = self.max_range_m
        for i, d in enumerate(self.distances_m):
            center = start + (i + 0.5) * sec
            # Wrap into [-180, 180] for clean abs-diff against bearing_deg.
            delta = ((center - bearing_deg + 180.0) % 360.0) - 180.0
            if abs(delta) <= half_angle_deg:
                # Treat NaN / negative as max_range (sensor garbage).
                if d is None or not math.isfinite(d) or d < 0:
                    continue
                if d < worst:
                    worst = d
        return worst
