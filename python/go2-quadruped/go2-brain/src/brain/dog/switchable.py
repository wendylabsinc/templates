"""SwitchableController — wraps multiple DogControllers, lets you flip
between them at runtime.

Used so the operator can `curl /mode/unitree` to enable real motion and
`curl /mode/mock` to fall back to dry-run without restarting the brain.
On any switch, the previous controller's `stop()` is called so the dog
doesn't keep moving on stale state.
"""

from __future__ import annotations

import logging
from threading import Lock

from .base import DogController

logger = logging.getLogger(__name__)


class SwitchableController(DogController):
    def __init__(
        self,
        controllers: dict[str, DogController],
        default: str,
    ) -> None:
        if default not in controllers:
            raise ValueError(f"default mode {default!r} not in {list(controllers)}")
        self._controllers = controllers
        self._current_name = default
        self._lock = Lock()
        logger.info(
            "init controllers=%s active=%s", list(controllers), default
        )

    @property
    def current_name(self) -> str:
        with self._lock:
            return self._current_name

    @property
    def available(self) -> list[str]:
        return list(self._controllers)

    def set_mode(self, name: str) -> str:
        with self._lock:
            if name not in self._controllers:
                raise ValueError(
                    f"unknown mode {name!r}; expected one of {list(self._controllers)}"
                )
            if name == self._current_name:
                return self._current_name
            previous = self._current_name
            # Halt the previous controller before swapping. Critical when
            # switching unitree → mock so the dog isn't left walking.
            try:
                self._controllers[previous].stop()
            except Exception:
                logger.exception("error stopping previous controller %s", previous)
            self._current_name = name
            logger.info("MODE %s→%s", previous, name)
            return name

    def _active(self) -> DogController:
        with self._lock:
            return self._controllers[self._current_name]

    def set_velocity(self, vx: float = 0.0, vy: float = 0.0, vyaw: float = 0.0) -> None:
        self._active().set_velocity(vx=vx, vy=vy, vyaw=vyaw)

    def stop(self) -> None:
        # Emergency: stop ALL controllers — never trust which is active
        # during shutdown / panic.
        for name, c in self._controllers.items():
            try:
                c.stop()
            except Exception:
                logger.exception("error stopping controller %s during emergency stop", name)

    def close(self) -> None:
        for name, c in self._controllers.items():
            try:
                c.close()
            except Exception:
                logger.exception("error closing controller %s", name)
