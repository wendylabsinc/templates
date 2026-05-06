"""UnitreeController — HTTP client to the go2-motion service.

The brain decides; go2-motion actuates. This class is the bridge.

Why HTTP and not unitree_sdk2_python directly? Three reasons:
  1. The SDK + cyclonedds C-build bloats the brain image by ~200 MB.
  2. Velocity caps + watchdog belong next to the hardware, not in the
     decision loop. go2-motion enforces them; brain can't bypass.
  3. Manual `curl` testing of the dog stays available even when brain
     isn't running.

The controller is non-blocking: each call fires a quick HTTP POST and
returns. Brain ticks at 10 Hz; HTTP over localhost on the Jetson takes a
couple of ms, well under the tick budget.

Phase-2 interface: a single `set_velocity(vx, vy, vyaw)`. The state
machine computes proportional values; we just relay them. We re-issue
every tick (even when zero) — go2-motion's watchdog is 1 s, so re-firing
every 100 ms keeps it warm. If the brain crashes or the network drops,
the watchdog stops the dog within 1 s.

`stop()` is the emergency endpoint — cancels any in-flight motion outright.
"""

from __future__ import annotations

import logging
import os

import httpx

from .base import DogController

logger = logging.getLogger(__name__)

MOTION_BASE_URL = os.environ.get("MOTION_BASE_URL", "http://127.0.0.1:3201")
HTTP_TIMEOUT_S = float(os.environ.get("BRAIN_HTTP_TIMEOUT_S", "0.2"))


class UnitreeController(DogController):
    def __init__(self) -> None:
        # Persistent client — keep-alive avoids paying TCP handshake on
        # every 10 Hz tick.
        self._client = httpx.Client(
            base_url=MOTION_BASE_URL,
            timeout=HTTP_TIMEOUT_S,
        )
        self._last_logged: tuple[float, float, float] | None = None
        logger.info("init base_url=%s", MOTION_BASE_URL)

    def _log_if_new(self, vx: float, vy: float, vyaw: float) -> None:
        key = (round(vx, 2), round(vy, 2), round(vyaw, 2))
        if key == self._last_logged:
            return
        self._last_logged = key
        logger.info("CMD vx=%+.2f vy=%+.2f vyaw=%+.2f", vx, vy, vyaw)

    def _post(self, path: str, json: dict | None = None) -> None:
        try:
            r = self._client.post(path, json=json or {})
            r.raise_for_status()
        except Exception as exc:
            # Don't crash the tick loop on transient HTTP issues — log
            # and let the watchdog on the motion side stop the dog if
            # this keeps failing.
            logger.warning("HTTP %s %s failed: %s", path, json, exc)

    def set_velocity(self, vx: float = 0.0, vy: float = 0.0, vyaw: float = 0.0) -> None:
        self._log_if_new(vx, vy, vyaw)
        self._post("/velocity", {"vx": vx, "vy": vy, "vyaw": vyaw})

    def stop(self) -> None:
        # Emergency stop — bypass dedupe so it always fires.
        self._last_logged = None
        logger.info("CMD STOP")
        self._post("/stop")

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            logger.exception("error closing httpx client")
