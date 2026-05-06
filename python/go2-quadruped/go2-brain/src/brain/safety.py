"""Safety wrapper between the FSM and the underlying DogController.

`SafetyController` decorates any inner `DogController` (typically a
`SwitchableController`) and clips `set_velocity` based on the current
LIDAR-derived `FreeSpace`. Yaw is always passed through — rotating in
place doesn't translate the body so it can't hit anything. Translational
velocity (`vx`, `vy`) is scaled down or zeroed when an obstacle is
inside the dog's reach in that direction.

Failure modes
-------------
- `free_space` is missing or stale (watchtower not publishing yet, or
  hasn't been deployed): default mode is **pass-through with a warning
  log**, so dev against today's go2-sim continues to work without
  watchtower changes. Set `BRAIN_SAFETY_STRICT=1` for fail-safe (zero
  translational velocity until free_space arrives) — recommended for
  real-dog field deploy.
- The wrapper never blocks `stop()`. Whatever happens, an emergency stop
  always reaches the inner controller.

Tunables (env vars; reasonable defaults)
----------------------------------------
- `BRAIN_SAFETY_MIN_DISTANCE_M`   (0.40) full clip at this range
- `BRAIN_SAFETY_RAMP_M`           (0.30) linear scale 0..1 over an extra
                                          this-many metres past min
- `BRAIN_SAFETY_CONE_HALF_DEG`    (30)   half-angle of the lookahead cone
                                          in the direction of travel
- `BRAIN_SAFETY_STRICT`           (0)    1 = require free_space, fail-safe

Telemetry
---------
`status()` returns a snapshot suitable for the heartbeat log and the
intent publisher: latest `min_ahead_m`, scale factors actually applied,
and whether the last call was clipped. Operators see "I commanded
vx=+0.30 but the dog only got vx=+0.10 because there's a wall" without
needing log-grep gymnastics.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from dataclasses import dataclass
from typing import Optional, Protocol

from .dog.base import DogController
from .models import FreeSpace

logger = logging.getLogger(__name__)

# Below this magnitude we treat a velocity component as "zero" — no clip
# work to do, no telemetry update. Saves a few `latest()` calls per tick
# during HOLD.
EPS = 1e-6


class FreeSpaceSource(Protocol):
    """Anything with a `latest()` returning Optional[FreeSpace]. The real
    implementation is `perception.FreeSpaceSubscriber`; tests pass a
    dummy that returns whatever they want. Keeping this as a Protocol
    avoids a circular import and makes the wrapper unit-testable without
    DDS."""

    def latest(self) -> Optional[FreeSpace]: ...
    def age_s(self) -> Optional[float]: ...


@dataclass(frozen=True)
class SafetyStatus:
    """Operator-facing snapshot of what the safety wrapper just did."""

    free_space_age_s: Optional[float]   # None = never seen any free_space
    min_ahead_m: Optional[float]        # closest obstacle in the +vx cone
    scale_vx: float                     # 0..1, factor actually applied to vx
    scale_vy: float                     # 0..1, factor actually applied to vy
    clipped: bool                       # any axis scaled below 1.0
    strict_blocked: bool                # True = strict mode + free_space missing


class SafetyController(DogController):
    """Free-space-aware velocity clipper. Wraps any inner DogController."""

    def __init__(
        self,
        inner: DogController,
        free_space: FreeSpaceSource,
        *,
        min_distance_m: Optional[float] = None,
        ramp_m: Optional[float] = None,
        cone_half_deg: Optional[float] = None,
        strict: Optional[bool] = None,
    ) -> None:
        self._inner = inner
        self._fs = free_space
        # Env-driven defaults; explicit kwargs override (handy for tests).
        self._min_distance_m = (
            min_distance_m
            if min_distance_m is not None
            else float(os.environ.get("BRAIN_SAFETY_MIN_DISTANCE_M", "0.40"))
        )
        self._ramp_m = (
            ramp_m
            if ramp_m is not None
            else float(os.environ.get("BRAIN_SAFETY_RAMP_M", "0.30"))
        )
        self._cone_half_deg = (
            cone_half_deg
            if cone_half_deg is not None
            else float(os.environ.get("BRAIN_SAFETY_CONE_HALF_DEG", "30.0"))
        )
        self._strict = (
            strict
            if strict is not None
            else os.environ.get("BRAIN_SAFETY_STRICT", "0") in ("1", "true", "True")
        )

        self._lock = threading.Lock()
        self._last_status = SafetyStatus(
            free_space_age_s=None,
            min_ahead_m=None,
            scale_vx=1.0,
            scale_vy=1.0,
            clipped=False,
            strict_blocked=False,
        )
        # Throttle the "free_space missing" warning so we don't spam the
        # log every tick while watchtower's LIDAR filter isn't deployed.
        self._missing_warned = False

        logger.info(
            "safety: min=%.2fm ramp=%.2fm cone=±%.0f° strict=%s",
            self._min_distance_m, self._ramp_m, self._cone_half_deg, self._strict,
        )

    # ------------------------------------------------------------------
    # DogController interface
    # ------------------------------------------------------------------

    def set_velocity(self, vx: float = 0.0, vy: float = 0.0, vyaw: float = 0.0) -> None:
        clipped_vx, clipped_vy, status = self._compute_clip(vx, vy)
        with self._lock:
            self._last_status = status
        # Yaw is always passed through — pure rotation can't translate
        # the body into an obstacle. (The body is round-ish from above.)
        self._inner.set_velocity(vx=clipped_vx, vy=clipped_vy, vyaw=vyaw)

    def stop(self) -> None:
        # Emergency stop ALWAYS goes through, no clipping.
        self._inner.stop()

    def close(self) -> None:
        self._inner.close()

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    @property
    def inner(self) -> DogController:
        """The wrapped controller. Mode-server uses this for /mode/<name>
        and /stop so its API targets the SwitchableController, not us."""
        return self._inner

    def status(self) -> SafetyStatus:
        with self._lock:
            return self._last_status

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _compute_clip(
        self, vx: float, vy: float,
    ) -> tuple[float, float, SafetyStatus]:
        """Return clipped (vx, vy) and a status snapshot."""
        # Resting commands or pure yaw need no work, but we still want to
        # surface free_space age in telemetry, so do a minimal probe.
        fs = self._fs.latest()
        fs_age = self._fs.age_s()

        # Probe the forward cone for telemetry even when not commanding
        # forward motion — operators want a continuous "obstacle ahead"
        # readout, not just one that updates when we're walking.
        min_ahead = (
            fs.min_distance_in_cone(0.0, self._cone_half_deg)
            if fs is not None else None
        )

        if abs(vx) < EPS and abs(vy) < EPS:
            return vx, vy, SafetyStatus(
                free_space_age_s=fs_age,
                min_ahead_m=min_ahead,
                scale_vx=1.0, scale_vy=1.0,
                clipped=False,
                strict_blocked=False,
            )

        # No free_space in hand → pass-through (default) or fail-safe (strict).
        if fs is None:
            if not self._missing_warned:
                logger.warning(
                    "safety: free_space missing/stale (age=%s) — %s",
                    f"{fs_age:.2f}s" if fs_age is not None else "never",
                    "blocking translation (strict)" if self._strict
                    else "passing velocities through",
                )
                self._missing_warned = True
            if self._strict:
                return 0.0, 0.0, SafetyStatus(
                    free_space_age_s=fs_age, min_ahead_m=None,
                    scale_vx=0.0, scale_vy=0.0,
                    clipped=True, strict_blocked=True,
                )
            return vx, vy, SafetyStatus(
                free_space_age_s=fs_age, min_ahead_m=None,
                scale_vx=1.0, scale_vy=1.0,
                clipped=False, strict_blocked=False,
            )

        # We have fresh free_space. Reset the missing-warn flag so the
        # next outage gets reported again.
        self._missing_warned = False

        # Per-axis clip. Each translational axis queries its own cone in
        # the body frame: +x = ahead (0°), -x = behind (180°), +y = left
        # (90°), -y = right (-90°).
        scale_vx = self._axis_scale(fs, vx, bearing_deg=0.0, neg_bearing_deg=180.0)
        scale_vy = self._axis_scale(fs, vy, bearing_deg=90.0, neg_bearing_deg=-90.0)

        out_vx = vx * scale_vx
        out_vy = vy * scale_vy
        clipped = scale_vx < 1.0 - EPS or scale_vy < 1.0 - EPS

        return out_vx, out_vy, SafetyStatus(
            free_space_age_s=fs_age,
            min_ahead_m=min_ahead,
            scale_vx=scale_vx, scale_vy=scale_vy,
            clipped=clipped, strict_blocked=False,
        )

    def _axis_scale(
        self,
        fs: FreeSpace,
        v: float,
        *,
        bearing_deg: float,
        neg_bearing_deg: float,
    ) -> float:
        """Scale factor (0..1) for one translational axis based on the
        nearest obstacle in the cone toward the direction of motion.

        Outside the obstacle's stop range (>= min + ramp): no scale (1.0).
        Inside the stop range (<= min):                    full clip (0.0).
        In between:                                         linear ramp."""
        if abs(v) < EPS:
            return 1.0
        bearing = bearing_deg if v > 0 else neg_bearing_deg
        nearest = fs.min_distance_in_cone(bearing, self._cone_half_deg)
        ramp_top = self._min_distance_m + self._ramp_m
        if nearest >= ramp_top:
            return 1.0
        if nearest <= self._min_distance_m:
            return 0.0
        # Linear ramp from min..ramp_top → 0..1.
        return (nearest - self._min_distance_m) / max(self._ramp_m, EPS)
