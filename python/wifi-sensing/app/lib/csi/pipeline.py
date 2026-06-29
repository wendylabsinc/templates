"""Orchestrates ingest -> buffers -> DSP -> AnalyticsFrame broadcast."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

from app.config import Config
from app.lib.csi import dsp
from app.lib.csi.buffer import BufferStore
from app.lib.csi.ingest import CSISource
from app.lib.csi.parser import parse_csi_data
from app.lib.csi.types import AnalyticsFrame

log = logging.getLogger(__name__)


class Pipeline:
    """Drains a CSI source into per-link buffers and derives analytics on a cadence."""

    def __init__(self, config: Config, now: Callable[[], float] = time.monotonic):
        self.config = config
        self._now = now
        self.store = BufferStore(capacity=config.buffer_capacity)
        self.latest: AnalyticsFrame | None = None
        self.baseline: float | None = self._load_baseline()
        self._subscribers: set[asyncio.Queue] = set()

    # ---- ingest -----------------------------------------------------------
    def ingest(self, payload: bytes) -> None:
        """Parse one raw payload and route it to its link buffer."""
        frame = parse_csi_data(payload, timestamp=self._now())
        if frame is None:
            self.store.mark_malformed()
            return
        self.store.add(frame)

    async def run(self, source: CSISource) -> None:
        """Start the source and drain its frames into buffers until cancelled."""
        await source.start()
        log.info("CSI ingest listening on UDP %s", self.config.udp_port)
        async for payload in source.frames():
            self.ingest(payload)

    # ---- analysis ---------------------------------------------------------
    def _primary_link(self) -> str | None:
        links = self.store.links()
        if not links:
            return None
        return max(links, key=lambda l: len(self.store.get(l)))

    def analyze(self) -> AnalyticsFrame:
        """Compute one AnalyticsFrame from the current buffers and broadcast it."""
        cfg = self.config
        now = self._now()
        frame = AnalyticsFrame(timestamp=now)
        frame.sensors = list(self.store.stats(now).values())

        # Per-link waterfall for the dashboard heatmap.
        for link in self.store.links():
            _, amps = self.store.get(link).window(cfg.vitals_window_s, now)
            if amps.shape[0] > 0:
                frame.waterfall[link] = dsp.waterfall(amps)

        primary = self._primary_link()
        if primary is not None:
            buf = self.store.get(primary)
            presence_amps = buf.resampled(cfg.analysis_rate_hz, cfg.presence_window_s, now)
            occupied, motion = dsp.presence_motion(
                presence_amps, self.baseline, cfg.motion_threshold
            )
            frame.occupied = occupied
            frame.motion = motion

            vitals_amps = buf.resampled(cfg.analysis_rate_hz, cfg.vitals_window_s, now)
            v = dsp.vitals(vitals_amps, cfg.analysis_rate_hz, motion)
            frame.breathing_bpm = v["breathing_bpm"]
            frame.breathing_conf = v["breathing_conf"]
            frame.heart_bpm = v["heart_bpm"]
            frame.heart_conf = v["heart_conf"]

        self.latest = frame
        self._broadcast(frame)
        return frame

    async def analyze_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.emit_interval_s)
            try:
                self.analyze()
            except Exception:  # never let analysis kill the loop
                log.exception("analyze() failed")

    # ---- calibration ------------------------------------------------------
    def calibrate(self) -> float:
        """Capture the current variance as the empty-room baseline and persist it."""
        now = self._now()
        primary = self._primary_link()
        if primary is None:
            raise RuntimeError("no sensor data to calibrate against")
        amps = self.store.get(primary).resampled(
            self.config.analysis_rate_hz, self.config.presence_window_s, now
        )
        self.baseline = dsp.baseline_variance(amps)
        self._save_baseline(self.baseline)
        return self.baseline

    def _baseline_path(self):
        return self.config.data_dir / "baseline.json"

    def _load_baseline(self) -> float | None:
        try:
            return float(json.loads(self._baseline_path().read_text())["baseline"])
        except (OSError, ValueError, KeyError):
            return None

    def _save_baseline(self, value: float) -> None:
        try:
            self.config.data_dir.mkdir(parents=True, exist_ok=True)
            self._baseline_path().write_text(json.dumps({"baseline": value}))
        except OSError:
            log.warning("could not persist baseline to %s", self._baseline_path())

    # ---- subscriptions ----------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _broadcast(self, frame: AnalyticsFrame) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                pass

    def status(self) -> dict:
        return {
            "udp_port": self.config.udp_port,
            "links": len(self.store.links()),
            "calibrated": self.baseline is not None,
            "baseline": self.baseline,
        }
