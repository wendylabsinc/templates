"""MockController: logs each velocity command, never moves a real dog.

Default in safe-mode rehearsal. The brain's tick loop calls set_velocity
every iteration with the FSM's computed vx/vyaw; this controller just
emits a log line so you can read what the brain decided.

Used in dry-run mode against go2-sim, or when you want to validate
behaviour against the real /go2/uwb/decision stream from watchtower
without granting the brain authority to move the dog.
"""

from __future__ import annotations

import logging

from .base import DogController

logger = logging.getLogger(__name__)

# Round velocities to this many decimal places before deciding whether to
# log a "new" command. Without rounding, every tiny proportional jitter
# produces a fresh log line and drowns the console.
LOG_ROUNDING = 2


class MockController(DogController):
    def __init__(self) -> None:
        self._last_logged: tuple[float, float, float] | None = None

    def set_velocity(self, vx: float = 0.0, vy: float = 0.0, vyaw: float = 0.0) -> None:
        key = (round(vx, LOG_ROUNDING), round(vy, LOG_ROUNDING), round(vyaw, LOG_ROUNDING))
        if key == self._last_logged:
            return
        self._last_logged = key
        logger.info("CMD vx=%+.2f vy=%+.2f vyaw=%+.2f", vx, vy, vyaw)

    def stop(self) -> None:
        self._last_logged = None
        logger.info("CMD STOP")
