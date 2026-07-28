"""Runtime configuration, sourced from environment variables with safe defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    udp_port: int = 5566
    analysis_rate_hz: float = 20.0
    presence_window_s: float = 4.0
    vitals_window_s: float = 30.0
    motion_threshold: float = 1.5
    buffer_capacity: int = 4096
    emit_interval_s: float = 1.0
    data_dir: Path = Path("/data")

    @classmethod
    def from_env(cls) -> "Config":
        def _f(name: str, default: float) -> float:
            try:
                return float(os.environ[name])
            except (KeyError, ValueError):
                return default

        def _i(name: str, default: int) -> int:
            try:
                return int(os.environ[name])
            except (KeyError, ValueError):
                return default

        return cls(
            udp_port=_i("CSI_UDP_PORT", 5566),
            analysis_rate_hz=_f("CSI_ANALYSIS_RATE_HZ", 20.0),
            presence_window_s=_f("PRESENCE_WINDOW_S", 4.0),
            vitals_window_s=_f("VITALS_WINDOW_S", 30.0),
            motion_threshold=_f("MOTION_THRESHOLD", 1.5),
            data_dir=Path(os.environ.get("CSI_DATA_DIR", "/data")),
        )
