"""Abstract dog controller.

Single primitive: `set_velocity(vx, vy, vyaw)`. The brain emits a velocity
vector each tick; the controller is responsible for delivering it (mock
just logs, unitree posts to go2-motion). `stop()` is reserved for an
explicit emergency halt — `set_velocity(0, 0, 0)` is "stand still but
keep the watchdog warm", whereas `stop()` cancels motion outright.

The earlier discrete actions (spin_left, advance, hold, …) are gone — the
state machine now computes proportional vx/vyaw, so the controller no
longer needs to know about named actions.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class DogController(ABC):
    @abstractmethod
    def set_velocity(self, vx: float = 0.0, vy: float = 0.0, vyaw: float = 0.0) -> None:
        """Send a velocity command. Watchdog is renewed on every call."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Emergency stop. Cancels any in-flight motion immediately."""
        ...

    def close(self) -> None:
        """Release any owned resources (HTTP clients, threads, etc).

        Default is a no-op; subclasses with persistent resources override.
        Called from main.py's shutdown path after stop().
        """
        return None
