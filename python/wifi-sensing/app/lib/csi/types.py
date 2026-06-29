"""Core data types shared across the CSI pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class CSIFrame:
    """A single parsed CSI record from one sensor link.

    ``amplitudes`` holds one magnitude per subcarrier (``sqrt(re^2 + im^2)``).
    """

    link_id: str
    timestamp: float
    rssi: int
    channel: int
    amplitudes: np.ndarray


@dataclass
class SensorStat:
    """Per-link health, surfaced on the Sensors page."""

    link_id: str
    rssi: int = 0
    channel: int = 0
    packets: int = 0
    last_seen: float = 0.0
    malformed: int = 0

    def to_dict(self) -> dict:
        return {
            "link_id": self.link_id,
            "rssi": self.rssi,
            "channel": self.channel,
            "packets": self.packets,
            "last_seen": self.last_seen,
            "malformed": self.malformed,
        }


@dataclass
class AnalyticsFrame:
    """One round of derived analytics, broadcast to dashboard clients."""

    timestamp: float
    occupied: bool = False
    motion: float = 0.0
    breathing_bpm: float | None = None
    breathing_conf: float = 0.0
    heart_bpm: float | None = None
    heart_conf: float = 0.0
    sensors: list[SensorStat] = field(default_factory=list)
    waterfall: dict[str, list[list[float]]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "occupied": self.occupied,
            "motion": self.motion,
            "breathing_bpm": self.breathing_bpm,
            "breathing_conf": self.breathing_conf,
            "heart_bpm": self.heart_bpm,
            "heart_conf": self.heart_conf,
            "sensors": [s.to_dict() for s in self.sensors],
            "waterfall": self.waterfall,
        }
