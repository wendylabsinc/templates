"""DDS publisher for /go2/brain/intent.

Mirrors the perception.py reader pattern: talk to CycloneDDS via the
`cyclonedds-python` wheel and use a `std_msgs/msg/String` whose `data`
field is JSON. Watchtower subscribes via foxglove_bridge so this shows
up as a topic in Foxglove like any other.

Schema (one JSON object per tick):

    {
      "ts_ns":         int   monotonic-ish nanosecond timestamp
      "mode":          str   "mock" | "unitree"
      "state":         str   "IDLE" | "SEARCHING" | "FOLLOWING" | "RECOVERING"
      "action":        str   "HOLD" | "SPIN_LEFT" | "SPIN_RIGHT" | "ADVANCE" | "BACK_OFF"
      "reason":        str   human-readable explanation
      "lost_streak":   int   ride-through counter (0 when sample is good)
      "vx":            float m/s, +ve = forward
      "vy":            float m/s, +ve = left
      "vyaw":          float rad/s, +ve = left
      "decision": {                   # mirror of the raw UWB decision (or None)
        "tracking_state":  str
        "bearing_deg":     float|null
        "distance_m":      float|null
        "confidence":      float
        "vision_agreement": str
      } | null,
      "fused": {                      # post-fusion verdict that drove the FSM
        "bearing_deg":     float
        "distance_m":      float
        "bearing_source":  str        # "uwb" | "vision" | "fused"
        "confidence":      float
        "tracking_state":  str
        "age_s":           float
        "is_followable":   bool
      } | null,
      "vision_track_id": str|null,    # active operator track ID, if any
      "trail_len":     int,           # number of stored ghost-trail points
      "safety": {                     # what the safety wrapper just did
        "free_space_age_s": float|null
        "min_ahead_m":      float|null  # closest LIDAR return in front cone
        "scale_vx":         float       # 0..1 factor actually applied to vx
        "scale_vy":         float
        "clipped":          bool        # any axis scaled below 1.0
        "strict_blocked":   bool        # strict mode + free_space missing
      } | null
    }

This is the operator's window into the brain — Foxglove can show state
transitions, plot vx/vyaw, and surface why each tick chose what it did.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

# NOTE: do NOT use `from __future__ import annotations` here. cyclonedds-python's
# IdlStruct normalizer resolves type hints by looking them up in the dataclass's
# defining module — `str` (a builtin) doesn't exist there, so PEP-563 string
# annotations make it raise "Type str cannot be resolved". Concrete types only
# in this file. Same constraint as perception.py.

from cyclonedds.domain import DomainParticipant
from cyclonedds.idl import IdlStruct
from cyclonedds.pub import DataWriter, Publisher
from cyclonedds.topic import Topic

from .fusion import FusedTarget
from .models import Decision
from .safety import SafetyStatus
from .state_machine import Tick

logger = logging.getLogger(__name__)

INTENT_TOPIC = "rt/go2/brain/intent"


@dataclass
class _StdMsgsString(IdlStruct, typename="std_msgs::msg::dds_::String_"):
    """Wire-compatible mirror of std_msgs/msg/String for CycloneDDS."""

    data: str = ""


class IntentPublisher:
    def __init__(self, domain: int = 0) -> None:
        self._dp = DomainParticipant(domain)
        self._topic = Topic(self._dp, INTENT_TOPIC, _StdMsgsString)
        self._pub = Publisher(self._dp)
        self._writer = DataWriter(self._pub, self._topic)
        logger.info("intent publisher ready on %s", INTENT_TOPIC)

    def publish(
        self,
        tick: Tick,
        mode: str,
        lost_streak: int,
        decision: Optional[Decision],
        fused: Optional[FusedTarget] = None,
        trail_len: int = 0,
        safety: Optional[SafetyStatus] = None,
    ) -> None:
        payload = {
            "ts_ns": time.time_ns(),
            "mode": mode,
            "state": tick.state.value,
            "action": tick.action.value,
            "reason": tick.reason,
            "lost_streak": int(lost_streak),
            "vx": float(tick.vx),
            "vy": float(tick.vy),
            "vyaw": float(tick.vyaw),
            "decision": None if decision is None else {
                "tracking_state": decision.tracking_state,
                "bearing_deg": decision.bearing_deg,
                "distance_m": decision.distance_m,
                "confidence": float(decision.confidence),
                "vision_agreement": decision.vision_agreement,
            },
            "fused": None if fused is None else {
                "bearing_deg": float(fused.bearing_deg),
                "distance_m": float(fused.distance_m),
                "bearing_source": fused.bearing_source,
                "confidence": float(fused.confidence),
                "tracking_state": fused.tracking_state,
                "age_s": float(fused.age_s),
                "is_followable": bool(fused.is_followable),
            },
            "vision_track_id": (
                fused.vision_track_id if fused is not None else None
            ),
            "trail_len": int(trail_len),
            "safety": None if safety is None else {
                "free_space_age_s": (
                    float(safety.free_space_age_s)
                    if safety.free_space_age_s is not None else None
                ),
                "min_ahead_m": (
                    float(safety.min_ahead_m)
                    if safety.min_ahead_m is not None else None
                ),
                "scale_vx": float(safety.scale_vx),
                "scale_vy": float(safety.scale_vy),
                "clipped": bool(safety.clipped),
                "strict_blocked": bool(safety.strict_blocked),
            },
        }
        try:
            self._writer.write(_StdMsgsString(data=json.dumps(payload)))
        except Exception as exc:
            # Telemetry must never crash the tick loop.
            logger.warning("intent publish failed: %s", exc)
