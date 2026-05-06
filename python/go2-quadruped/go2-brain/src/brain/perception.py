"""DDS subscribers for the brain's perception inputs.

Four String/JSON contracts, all consumed via CycloneDDS without rclpy:

    /go2/uwb/decision                 (existing) UWB Kalman filter verdict
    /go2/vision/tracked_persons_json  (added)    flat per-track snapshot
    /go2/dog/pose_json                (added)    world-frame body pose
    /go2/perception/free_space        (added)    polar obstacle map (LIDAR)

go2-Watchtower (or go2-sim during off-dog dev) publishes all of them. The
brain treats vision, pose, and free_space as *optional* — if they're
silent, fusion falls back to UWB-only, the ghost trail is disabled, and
the safety wrapper passes velocities through unchanged. Schemas for the
non-Decision topics are mirrored in `models.py`.

We avoid pulling in rclpy (and its ROS install) by talking directly to
CycloneDDS via the `cyclonedds-python` wheel. The IDL declared here
matches the on-wire format ROS2 uses for `std_msgs/String`:
    typename `std_msgs::msg::dds_::String_`, single `string data`
    field. Topic name has the `rt/` prefix that ROS2 maps onto.

When schema fields are added on the publisher side, mirror them in the
dataclasses below.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

# NOTE: do NOT use `from __future__ import annotations` here. cyclonedds-python's
# IdlStruct normalizer resolves type hints by looking them up in the dataclass's
# defining module — `str` (a builtin) doesn't exist there, so PEP-563 string
# annotations make it raise "Type str cannot be resolved". Concrete types only
# in this file.

from cyclonedds.domain import DomainParticipant
from cyclonedds.idl import IdlStruct
from cyclonedds.sub import DataReader, Subscriber
from cyclonedds.topic import Topic

# Pure-data dataclasses live in models.py so tests + tooling can import
# them without dragging in CycloneDDS. Re-exported here for back-compat
# with code that already does `from brain.perception import Decision`.
from .models import Decision, FreeSpace, Pose, VisionTrack, VisionTracks

__all__ = [
    "Decision",
    "DecisionSubscriber",
    "FreeSpace",
    "FreeSpaceSubscriber",
    "Pose",
    "PoseSubscriber",
    "VisionTrack",
    "VisionTracks",
    "VisionTracksSubscriber",
]

logger = logging.getLogger(__name__)

DECISION_TOPIC = "rt/go2/uwb/decision"
VISION_TRACKS_TOPIC = "rt/go2/vision/tracked_persons_json"
POSE_TOPIC = "rt/go2/dog/pose_json"
FREE_SPACE_TOPIC = "rt/go2/perception/free_space"

# When the most recent decision is older than this, treat the stream as
# dead and return None from `latest()`. The brain's existing bad-sample
# handling (ride-through → recovery → IDLE) takes over from there. We
# stamp arrival on the brain side so this works regardless of how
# watchtower computes its own stamp_ns (which is per-process monotonic
# and not comparable across processes).
DECISION_STALE_S = float(os.environ.get("BRAIN_DECISION_STALE_S", "0.5"))
# Per-stream stalenesses. Vision is the slowest channel (CPU YOLO ~5–10 Hz)
# so we tolerate a wider window before treating it as gone. Pose is the
# fastest (20 Hz from sportmodestate) — half-second stale means the dog
# stopped publishing motion state, which is itself a fault we want to
# notice quickly.
VISION_STALE_S = float(os.environ.get("BRAIN_VISION_STALE_S", "0.4"))
POSE_STALE_S = float(os.environ.get("BRAIN_POSE_STALE_S", "0.5"))
# LIDAR scans are typically 10 Hz on the Go2; 0.5 s tolerates a couple of
# missed scans before the safety wrapper treats free_space as gone.
FREE_SPACE_STALE_S = float(os.environ.get("BRAIN_FREE_SPACE_STALE_S", "0.5"))


@dataclass
class _StdMsgsString(IdlStruct, typename="std_msgs::msg::dds_::String_"):
    """Wire-compatible mirror of `std_msgs/msg/String` for CycloneDDS."""

    data: str = ""


class DecisionSubscriber:
    """Latches the latest /go2/uwb/decision payload.

    Reading happens on a CycloneDDS reader thread. The state machine polls
    `latest()` from the main thread; we don't block waiting for a sample,
    we just return whatever was last received (or `None` if nothing yet).
    """

    def __init__(self, domain: int = 0) -> None:
        self._dp = DomainParticipant(domain)
        self._topic = Topic(self._dp, DECISION_TOPIC, _StdMsgsString)
        self._sub = Subscriber(self._dp)
        self._reader = DataReader(self._sub, self._topic)
        self._lock = threading.Lock()
        self._latest: Optional[Decision] = None
        self._latest_at: Optional[float] = None  # time.monotonic() of arrival
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def latest(self) -> Optional[Decision]:
        """Return the latest decision, or None if it's older than
        DECISION_STALE_S (or none has ever arrived). Brain's bad-sample
        path takes over when this returns None for a stale stream."""
        with self._lock:
            if self._latest is None or self._latest_at is None:
                return None
            if (time.monotonic() - self._latest_at) > DECISION_STALE_S:
                return None
            return self._latest

    def age_s(self) -> Optional[float]:
        """Seconds since the last decision arrived, or None if none yet.
        Useful for telemetry/diagnostics — the readiness check and intent
        publisher consume this."""
        with self._lock:
            if self._latest_at is None:
                return None
            return time.monotonic() - self._latest_at

    def _loop(self) -> None:
        while not self._stop.is_set():
            # Blocking take with a short timeout so we wake to check _stop
            # periodically. cyclonedds-python's iter API yields samples as
            # they arrive; we use the simpler poll-style here.
            samples = self._reader.take(N=10)
            saw_valid = False
            for sample in samples:
                # `take()` can yield InvalidSample objects (no `.data`)
                # when a publisher goes away — common when sim is
                # restarted while brain stays up. Skip them; the next
                # iteration will pick up fresh valid samples.
                data = getattr(sample, "data", None)
                if data is None:
                    continue
                decision = Decision.from_json(data)
                with self._lock:
                    self._latest = decision
                    self._latest_at = time.monotonic()
                saw_valid = True
            if not saw_valid:
                self._stop.wait(0.05)


# ---------------------------------------------------------------------------
# Vision tracker subscriber
# ---------------------------------------------------------------------------


class VisionTracksSubscriber:
    """Latches the latest /go2/vision/tracked_persons_json payload.

    Same daemon-thread pattern as DecisionSubscriber. `latest()` returns
    None when the stream has been silent past VISION_STALE_S — callers
    treat that as "no vision input" rather than "operator absent".
    """

    def __init__(self, domain: int = 0) -> None:
        self._dp = DomainParticipant(domain)
        self._topic = Topic(self._dp, VISION_TRACKS_TOPIC, _StdMsgsString)
        self._sub = Subscriber(self._dp)
        self._reader = DataReader(self._sub, self._topic)
        self._lock = threading.Lock()
        self._latest: Optional[VisionTracks] = None
        self._latest_at: Optional[float] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def latest(self) -> Optional[VisionTracks]:
        with self._lock:
            if self._latest is None or self._latest_at is None:
                return None
            if (time.monotonic() - self._latest_at) > VISION_STALE_S:
                return None
            return self._latest

    def age_s(self) -> Optional[float]:
        with self._lock:
            if self._latest_at is None:
                return None
            return time.monotonic() - self._latest_at

    def _loop(self) -> None:
        while not self._stop.is_set():
            samples = self._reader.take(N=10)
            saw_valid = False
            for sample in samples:
                data = getattr(sample, "data", None)
                if data is None:
                    continue
                tracks = VisionTracks.from_json(data)
                with self._lock:
                    self._latest = tracks
                    self._latest_at = time.monotonic()
                saw_valid = True
            if not saw_valid:
                self._stop.wait(0.05)


# ---------------------------------------------------------------------------
# Pose subscriber
# ---------------------------------------------------------------------------


class PoseSubscriber:
    """Latches the latest /go2/dog/pose_json payload."""

    def __init__(self, domain: int = 0) -> None:
        self._dp = DomainParticipant(domain)
        self._topic = Topic(self._dp, POSE_TOPIC, _StdMsgsString)
        self._sub = Subscriber(self._dp)
        self._reader = DataReader(self._sub, self._topic)
        self._lock = threading.Lock()
        self._latest: Optional[Pose] = None
        self._latest_at: Optional[float] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def latest(self) -> Optional[Pose]:
        with self._lock:
            if self._latest is None or self._latest_at is None:
                return None
            if (time.monotonic() - self._latest_at) > POSE_STALE_S:
                return None
            return self._latest

    def age_s(self) -> Optional[float]:
        with self._lock:
            if self._latest_at is None:
                return None
            return time.monotonic() - self._latest_at

    def _loop(self) -> None:
        while not self._stop.is_set():
            samples = self._reader.take(N=10)
            saw_valid = False
            for sample in samples:
                data = getattr(sample, "data", None)
                if data is None:
                    continue
                pose = Pose.from_json(data)
                with self._lock:
                    self._latest = pose
                    self._latest_at = time.monotonic()
                saw_valid = True
            if not saw_valid:
                self._stop.wait(0.05)


# ---------------------------------------------------------------------------
# Free-space subscriber (LIDAR-derived obstacle map)
# ---------------------------------------------------------------------------


class FreeSpaceSubscriber:
    """Latches the latest /go2/perception/free_space payload.

    Same daemon-thread pattern as the others. `latest()` returns None
    when the stream has been silent past FREE_SPACE_STALE_S — the safety
    wrapper falls back to "no clip" (or strict-mode if configured).
    """

    def __init__(self, domain: int = 0) -> None:
        self._dp = DomainParticipant(domain)
        self._topic = Topic(self._dp, FREE_SPACE_TOPIC, _StdMsgsString)
        self._sub = Subscriber(self._dp)
        self._reader = DataReader(self._sub, self._topic)
        self._lock = threading.Lock()
        self._latest: Optional[FreeSpace] = None
        self._latest_at: Optional[float] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def latest(self) -> Optional[FreeSpace]:
        with self._lock:
            if self._latest is None or self._latest_at is None:
                return None
            if (time.monotonic() - self._latest_at) > FREE_SPACE_STALE_S:
                return None
            return self._latest

    def age_s(self) -> Optional[float]:
        with self._lock:
            if self._latest_at is None:
                return None
            return time.monotonic() - self._latest_at

    def _loop(self) -> None:
        while not self._stop.is_set():
            samples = self._reader.take(N=10)
            saw_valid = False
            for sample in samples:
                data = getattr(sample, "data", None)
                if data is None:
                    continue
                free_space = FreeSpace.from_json(data)
                with self._lock:
                    self._latest = free_space
                    self._latest_at = time.monotonic()
                saw_valid = True
            if not saw_valid:
                self._stop.wait(0.05)
