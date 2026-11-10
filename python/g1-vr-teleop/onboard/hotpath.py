"""Small, reversible scheduling controls for the Python command hot path."""
from __future__ import annotations

import math
import os
import time


class DeadlineLoop:
    """Wait on an absolute cadence and drop missed ticks instead of bursting.

    Relative sleeps accumulate every scheduling delay.  At 100 Hz, a recurring
    1--2 ms GIL delay turns a nominal 10 ms loop into an 11--12 ms loop forever.
    This clock stays anchored; after a true overrun it advances to the first
    future deadline, so it never sends a catch-up burst to the robot.
    """

    def __init__(self, period, *, clock=time.monotonic, sleep=time.sleep):
        self.period = float(period)
        if not math.isfinite(self.period) or self.period <= 0:
            raise ValueError("period must be finite and positive")
        self._clock = clock
        self._sleep = sleep
        self._next = clock() + self.period
        self.missed = 0

    def wait(self):
        now = self._clock()
        if now > self._next:
            skipped = math.ceil((now - self._next) / self.period)
            self._next += skipped * self.period
            self.missed += skipped
        if now < self._next:
            self._sleep(self._next - now)
        self._next += self.period


def cpu_from_env(env, name, *, available=None, default=None, auto_offset=0):
    """Resolve an optional Linux CPU affinity: unset, integer, or ``auto``."""
    raw = str(env.get(name, default or "")).strip().lower()
    if not raw or raw in ("off", "none"):
        return None
    if available is None:
        available = os.sched_getaffinity(0)
    available = sorted(int(cpu) for cpu in available)
    if not available:
        raise ValueError(f"{name}: no CPUs are available")
    if raw == "auto":
        return available[max(0, len(available) - 1 - int(auto_offset))]
    try:
        cpu = int(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r}: expected auto or a CPU number") from None
    if cpu not in available:
        raise ValueError(f"{name}={cpu}: expected one of {available}")
    return cpu


def pin_current_thread(env_name, *, env=None, default=None, auto_offset=0):
    """Pin only the calling Linux thread. Returns a status dictionary."""
    env = os.environ if env is None else env
    if not hasattr(os, "sched_setaffinity"):
        return {"requested": env.get(env_name) or None, "cpu": None,
                "active": False, "error": "unsupported platform"}
    try:
        cpu = cpu_from_env(env, env_name, default=default, auto_offset=auto_offset)
        if cpu is None:
            return {"requested": None, "cpu": None, "active": False, "error": None}
        os.sched_setaffinity(0, {cpu})
        return {"requested": env.get(env_name, default), "cpu": cpu,
                "active": True, "error": None}
    except (OSError, ValueError) as exc:
        return {"requested": env.get(env_name) or None, "cpu": None,
                "active": False, "error": str(exc)}
