"""Body-frame ghost trail of the operator's recent positions.

Storage is world-frame (anchored to /go2/dog/pose_json) so the math is
numerically stable; the read interface is body-frame so the FSM and
control law keep using the same coordinates they always have.

Two phases drive the recovery goal:

    Phase A (0 ≤ t < TRAIL_APPROACH_S)
        Goal = newest trail entry, transformed to current body frame.
        The dog drives to where it last saw the operator.

    Phase B (APPROACH_S ≤ t < APPROACH_S + EXTRAPOLATE_S)
        Estimate the operator's world-frame velocity from the last
        TANGENT_WINDOW_S of the trail and extrapolate forward. Drives
        the dog past the corner.

    After Phase B
        Returns None. The FSM gives up and falls back to IDLE.

If `pose` is unavailable (sportmodestate stream silent), `append()` is
a no-op and `goal_for_recovery()` returns None — the FSM then falls back
to today's blind-spin behavior so we don't make recovery worse than it
was.

Pure data + math — no DDS, no I/O. Tested in isolation.
"""

from __future__ import annotations

import math
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple

from .fusion import FusedTarget
from .models import Pose


TRAIL_HISTORY_S = float(os.environ.get("BRAIN_TRAIL_HISTORY_S", "4.0"))
TRAIL_MAX_ENTRIES = int(os.environ.get("BRAIN_TRAIL_MAX_ENTRIES", "120"))
TRAIL_APPROACH_S = float(os.environ.get("BRAIN_TRAIL_APPROACH_S", "1.5"))
TRAIL_EXTRAPOLATE_S = float(os.environ.get("BRAIN_TRAIL_EXTRAPOLATE_S", "2.5"))
TANGENT_WINDOW_S = float(
    os.environ.get("BRAIN_TRAIL_TANGENT_WINDOW_S", "0.8")
)
# Cap the extrapolation tangent so a glitchy last-second pose doesn't
# fling the goal off into orbit. The operator can't run faster than ~3
# m/s anyway.
MAX_TANGENT_MPS = float(os.environ.get("BRAIN_TRAIL_MAX_TANGENT_MPS", "3.0"))


@dataclass
class TrailGoal:
    """Body-frame goal point during RECOVERING. The FSM feeds this to
    the same proportional control law that drives FOLLOWING."""

    body_x: float
    body_y: float
    age_s: float          # how stale the underlying observation is
    extrapolated: bool    # True for synthesised Phase-B points


@dataclass
class _Entry:
    """One operator observation, stored in world frame."""

    t: float
    x_world: float
    y_world: float


class GhostTrail:
    """Records operator positions during FOLLOWING; replays them as
    body-frame waypoints during RECOVERING. Owns no threads — call
    `append()` from the same loop that calls `goal_for_recovery()`."""

    def __init__(self) -> None:
        self._entries: Deque[_Entry] = deque()

    def __len__(self) -> int:
        return len(self._entries)

    def reset(self) -> None:
        self._entries.clear()

    def append(self, target: FusedTarget, pose: Optional[Pose]) -> None:
        """Store the operator's current world-frame position. Drops the
        sample silently if no pose is available — the trail is a
        best-effort feature that requires sportmodestate to be live."""
        if pose is None:
            return
        body_x = target.distance_m * math.cos(math.radians(target.bearing_deg))
        body_y = target.distance_m * math.sin(math.radians(target.bearing_deg))
        cos_y = math.cos(pose.yaw_rad)
        sin_y = math.sin(pose.yaw_rad)
        x_world = pose.x_m + cos_y * body_x - sin_y * body_y
        y_world = pose.y_m + sin_y * body_x + cos_y * body_y
        now = time.monotonic()
        self._entries.append(
            _Entry(t=now, x_world=x_world, y_world=y_world)
        )
        self._prune(now)

    def goal_for_recovery(
        self, pose: Optional[Pose], elapsed_recover_s: float
    ) -> Optional[TrailGoal]:
        """Return the next body-frame waypoint for RECOVERING, or None
        when the trail is empty / pose is missing / extrapolation has
        run out."""
        if pose is None or not self._entries:
            return None

        if elapsed_recover_s < TRAIL_APPROACH_S:
            newest = self._entries[-1]
            bx, by = self._world_to_body(pose, newest.x_world, newest.y_world)
            return TrailGoal(
                body_x=bx,
                body_y=by,
                age_s=time.monotonic() - newest.t,
                extrapolated=False,
            )

        elapsed_phase_b = elapsed_recover_s - TRAIL_APPROACH_S
        if elapsed_phase_b > TRAIL_EXTRAPOLATE_S:
            return None

        tangent = self._world_tangent()
        newest = self._entries[-1]
        if tangent is None:
            bx, by = self._world_to_body(pose, newest.x_world, newest.y_world)
            return TrailGoal(
                body_x=bx,
                body_y=by,
                age_s=time.monotonic() - newest.t,
                extrapolated=True,
            )

        vx_w, vy_w = tangent
        x_w = newest.x_world + vx_w * elapsed_phase_b
        y_w = newest.y_world + vy_w * elapsed_phase_b
        bx, by = self._world_to_body(pose, x_w, y_w)
        return TrailGoal(
            body_x=bx,
            body_y=by,
            age_s=elapsed_phase_b,
            extrapolated=True,
        )

    # -- internals ----------------------------------------------------------

    def _prune(self, now: float) -> None:
        cutoff = now - TRAIL_HISTORY_S
        while self._entries and self._entries[0].t < cutoff:
            self._entries.popleft()
        while len(self._entries) > TRAIL_MAX_ENTRIES:
            self._entries.popleft()

    def _world_tangent(self) -> Optional[Tuple[float, float]]:
        """Return the operator's recent world-frame velocity, capped at
        MAX_TANGENT_MPS so a noisy last-second pose can't synthesize a
        rocket trajectory."""
        if len(self._entries) < 2:
            return None
        newest = self._entries[-1]
        cutoff_t = newest.t - TANGENT_WINDOW_S
        # Find the oldest entry within the tangent window. (Walk
        # newest-to-oldest and pick the first one outside the window;
        # otherwise use the actual oldest.)
        oldest = self._entries[0]
        for e in reversed(self._entries):
            if e.t <= cutoff_t:
                oldest = e
                break
        dt = newest.t - oldest.t
        if dt < 1e-3:
            return None
        vx = (newest.x_world - oldest.x_world) / dt
        vy = (newest.y_world - oldest.y_world) / dt
        speed = math.hypot(vx, vy)
        if speed > MAX_TANGENT_MPS:
            scale = MAX_TANGENT_MPS / speed
            vx *= scale
            vy *= scale
        return vx, vy

    @staticmethod
    def _world_to_body(
        pose: Pose, x_w: float, y_w: float
    ) -> Tuple[float, float]:
        dx = x_w - pose.x_m
        dy = y_w - pose.y_m
        cos_y = math.cos(-pose.yaw_rad)
        sin_y = math.sin(-pose.yaw_rad)
        body_x = cos_y * dx - sin_y * dy
        body_y = sin_y * dx + cos_y * dy
        return body_x, body_y
