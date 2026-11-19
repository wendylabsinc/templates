"""Small, bounded temperature trend monitor for the operator health UI."""
from __future__ import annotations

from collections import deque
import math
import threading
import time


class ThermalRiseMonitor:
    """Track sustained per-joint temperature rise over a short rolling window.

    Temperatures are quantized by the motor firmware, so a single delta is
    noisy. A least-squares slope over at least four seconds catches a real
    climb without turning one integer-degree step into an alarm.
    """

    WINDOW_S = 8.0
    MIN_SPAN_S = 4.0
    WARN_C_PER_S = 0.25
    BAD_C_PER_S = 0.50
    STALE_S = 2.0
    MIN_SAMPLE_S = 0.08

    def __init__(self):
        self._samples = {}
        self._latest = {}
        self._last_sample_t = None
        self._lock = threading.Lock()

    @staticmethod
    def _valid(value):
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value)))

    def observe(self, readings, now=None):
        now = time.monotonic() if now is None else float(now)
        clean = {str(name): float(value) for name, value in (readings or {}).items()
                 if self._valid(value)}
        with self._lock:
            self._latest = {name: (value, now) for name, value in clean.items()}
            if self._last_sample_t is not None and now - self._last_sample_t < self.MIN_SAMPLE_S:
                return
            self._last_sample_t = now
            cutoff = now - self.WINDOW_S
            for name, value in clean.items():
                samples = self._samples.setdefault(name, deque())
                samples.append((now, value))
                while samples and samples[0][0] < cutoff:
                    samples.popleft()
            for name in list(self._samples):
                samples = self._samples[name]
                while samples and samples[0][0] < cutoff:
                    samples.popleft()
                if not samples:
                    del self._samples[name]

    @staticmethod
    def _slope(samples):
        if len(samples) < 2 or samples[-1][0] - samples[0][0] < ThermalRiseMonitor.MIN_SPAN_S:
            return None
        mean_t = sum(t for t, _ in samples) / len(samples)
        mean_v = sum(v for _, v in samples) / len(samples)
        denom = sum((t - mean_t) ** 2 for t, _ in samples)
        if denom <= 0:
            return None
        return sum((t - mean_t) * (v - mean_v) for t, v in samples) / denom

    def status(self, now=None):
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            latest = dict(self._latest)
            samples = {name: tuple(points) for name, points in self._samples.items()}
        current = {name: value for name, (value, stamp) in latest.items()
                   if now - stamp <= self.STALE_S}
        rates = {name: self._slope(samples.get(name, ())) for name in current}
        candidates = [(rate, name) for name, rate in rates.items()
                      if rate is not None and rate >= self.WARN_C_PER_S]
        fastest = None
        if candidates:
            rate, name = max(candidates)
            fastest = {
                "joint": name,
                "temperature_c": round(current[name], 1),
                "rise_c_per_s": round(rate, 3),
                "level": "bad" if rate >= self.BAD_C_PER_S else "warn",
            }
        return {
            "monitored_joints": len(current),
            "fastest": fastest,
            "warn_c_per_s": self.WARN_C_PER_S,
            "bad_c_per_s": self.BAD_C_PER_S,
            "window_s": self.WINDOW_S,
        }
