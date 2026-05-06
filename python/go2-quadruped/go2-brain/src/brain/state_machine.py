"""Follow-me state machine.

Consumes a `FusedTarget` (UWB ⊕ vision) plus an optional `TrailGoal`
during recovery, and produces a velocity command (vx, vy, vyaw) plus
a labelling `Action` per tick.

States:
    IDLE        no usable target (LOST and recovery exhausted).
    SEARCHING   target is ACQUIRING — wait for confidence to climb.
    FOLLOWING   target is TRACKING/PREDICTING and confidence is high
                enough to act on. Bearing source can be UWB, vision, or
                fused — the FSM doesn't care which.
    RECOVERING  ride-through expired while FOLLOWING. If a ghost-trail
                goal is available we drive toward the operator's last
                observed positions (Phase A → Phase B extrapolation);
                otherwise fall back to spin-toward-last-bearing. A
                fresh good sample returns to FOLLOWING; a timeout drops
                to IDLE.

Action labels (for logs and the /go2/brain/intent topic):
    HOLD       stay put; either no target or target is already where
               we want it.
    SPIN_LEFT, SPIN_RIGHT  yaw correction in place.
    ADVANCE    closing distance (vx > 0).
    BACK_OFF   too close, backing up (vx < 0).

Control law (proportional):
    vyaw = clip(k_yaw · bearing_rad, ±max_yaw_rate)   [zero inside bearing dead-band]
    vx   = clip(k_dist · (distance - target), ±max_vx)   only when bearing
           is within ±aligned_half_deg of forward — otherwise spin in place.

The same law drives ghost-trail recovery: a TrailGoal is a body-frame
point, which we convert to (bearing_deg, distance_m) and feed into the
proportional controller exactly like a real target.

Tunables live on the `Params` dataclass and are owned by a `ParamStore`
that the operator can mutate at runtime via the mode-server's /params
endpoint. Each `step()` snapshots params once so a tick is internally
consistent even if an update lands mid-tick.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .fusion import FusedTarget
from .trail import TrailGoal

logger = logging.getLogger(__name__)

# Where on disk we persist live-tuned param overrides. The intent is:
#   - env vars (BRAIN_*) are the immutable baseline / fallback;
#   - on every /params POST we write the current values here;
#   - on startup we load this file (if present) on top of from_env, so a
#     brain restart keeps whatever the operator had dialled in last.
# Override with BRAIN_PARAMS_PATH. Inside Docker, the default path lives
# in $HOME (/root/...) which is wiped on container recreate — for true
# cross-restart persistence on the dog, mount a host volume into that
# directory or set BRAIN_PARAMS_PATH to a path on a mounted volume.
DEFAULT_PARAMS_PATH = Path.home() / ".go2-brain" / "params.json"
PARAMS_PATH = Path(
    os.environ.get("BRAIN_PARAMS_PATH", str(DEFAULT_PARAMS_PATH))
)


@dataclass(frozen=True)
class Params:
    """All FSM + control-law tunables. Frozen so snapshots are safe to
    share between threads without defensive copies."""

    # Control law
    target_distance_m: float = 0.45
    distance_dead_band_m: float = 0.10
    bearing_dead_band_deg: float = 20.0
    aligned_half_deg: float = 45.0
    # Tighter cone used when UWB is the only bearing source. Field
    # measurements showed PDoA-induced bearing biases of ~50° from
    # tag rotation alone, so the dog should spin to "near-zero
    # apparent bearing" before walking — by then the operator is in
    # the camera FOV and vision takes over. See `_compute_velocity`.
    uwb_only_aligned_half_deg: float = 15.0
    k_yaw: float = 1.0       # rad/s per rad bearing error
    k_dist: float = 0.5      # m/s per m distance error
    max_yaw_rate: float = 0.7  # rad/s
    max_vx: float = 0.35       # m/s

    # FSM gates
    enter_following_confidence: float = 0.4
    exit_following_confidence: float = 0.3

    # Ride-through + recovery
    lost_ride_through_ticks: int = 5
    recovery_timeout_s: float = 5.0
    recovery_yaw_rate: float = 0.4

    @classmethod
    def from_env(cls) -> "Params":
        """Construct from BRAIN_* env vars, falling back to defaults.

        Env-var names mirror the existing convention so older deployments
        keep working without changes."""
        return cls(
            target_distance_m=float(os.environ.get("BRAIN_TARGET_DISTANCE_M", "0.45")),
            distance_dead_band_m=float(os.environ.get("BRAIN_DISTANCE_DEAD_BAND_M", "0.10")),
            bearing_dead_band_deg=float(os.environ.get("BRAIN_BEARING_DEAD_BAND_DEG", "20.0")),
            aligned_half_deg=float(os.environ.get("BRAIN_ALIGNED_HALF_DEG", "45")),
            uwb_only_aligned_half_deg=float(
                os.environ.get("BRAIN_UWB_ONLY_ALIGNED_HALF_DEG", "15")
            ),
            k_yaw=float(os.environ.get("BRAIN_K_YAW", "1.0")),
            k_dist=float(os.environ.get("BRAIN_K_DIST", "0.5")),
            max_yaw_rate=float(os.environ.get("BRAIN_MAX_YAW_RATE", "0.7")),
            max_vx=float(os.environ.get("BRAIN_MAX_VX", "0.35")),
            enter_following_confidence=float(
                os.environ.get("BRAIN_ENTER_FOLLOWING_CONFIDENCE", "0.4")
            ),
            exit_following_confidence=float(
                os.environ.get("BRAIN_EXIT_FOLLOWING_CONFIDENCE", "0.3")
            ),
            lost_ride_through_ticks=int(
                os.environ.get("BRAIN_LOST_RIDE_THROUGH_TICKS", "5")
            ),
            recovery_timeout_s=float(os.environ.get("BRAIN_RECOVERY_TIMEOUT_S", "5.0")),
            recovery_yaw_rate=float(os.environ.get("BRAIN_RECOVERY_YAW_RATE", "0.4")),
        )

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Params":
        """Return env defaults overlaid with valid persisted values.

        Read order: env (baseline) ← persisted file (overrides). Any keys
        that are unknown, mistyped, or out-of-bounds in the file are logged
        and ignored — we'd rather start with env defaults than refuse to
        boot because someone hand-edited a value to junk."""
        path = path if path is not None else PARAMS_PATH
        base = cls.from_env()
        if not path.exists():
            return base
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "params: failed to read %s (%s) — using env defaults", path, exc
            )
            return base
        if not isinstance(raw, dict):
            logger.warning("params: %s is not a JSON object — using env defaults", path)
            return base

        overlay: dict[str, Any] = {}
        for name, value in raw.items():
            if name not in PARAM_BOUNDS:
                logger.warning("params: ignoring unknown persisted key %r", name)
                continue
            existing = getattr(base, name)
            try:
                coerced = type(existing)(value)
            except (TypeError, ValueError):
                logger.warning(
                    "params: ignoring persisted %s=%r (not a %s)",
                    name, value, type(existing).__name__,
                )
                continue
            lo, hi, _, _, _ = PARAM_BOUNDS[name]
            if not (lo <= coerced <= hi):
                logger.warning(
                    "params: ignoring persisted %s=%s (out of bounds [%s, %s])",
                    name, coerced, lo, hi,
                )
                continue
            overlay[name] = coerced

        if overlay:
            logger.info("params: loaded %d override(s) from %s", len(overlay), path)
        return dataclasses.replace(base, **overlay)


# Slider/UI metadata for each param: (min, max, step, label, info).
# `info` is the plain-English tooltip the operator UI shows on hover/tap —
# written for someone who is good at robots but bad at jargon.
# Bounds are intentionally wide so operators can experiment in sim — they
# are not hardware safety limits (those belong on go2-motion). Each entry
# also drives the operator UI rendering and validation in ParamStore.update.
PARAM_BOUNDS: dict[str, tuple[float, float, float, str, str]] = {
    "target_distance_m": (
        0.2, 3.0, 0.05, "Follow distance (m)",
        "How far behind the person the dog tries to stay. "
        "Smaller = closer follow; bigger = more breathing room.",
    ),
    "distance_dead_band_m": (
        0.0, 0.5, 0.01, "Distance dead-band (m)",
        "If the dog is within this much of the target distance, it stops "
        "creeping forward or back. Stops it jittering around the sweet spot.",
    ),
    "bearing_dead_band_deg": (
        0.0, 60.0, 1.0, "Bearing dead-band (°)",
        "If the person is within this many degrees of straight ahead, the "
        "dog doesn't bother turning. Stops it twitching to chase tiny angles.",
    ),
    "aligned_half_deg": (
        10.0, 90.0, 1.0, "Aligned cone half-angle (°)",
        "The dog only walks forward when the person is inside this cone in "
        "front of it. Bigger = walks forward even at sharp angles; "
        "smaller = spins to face first, then walks.",
    ),
    "uwb_only_aligned_half_deg": (
        5.0, 60.0, 1.0, "UWB-only aligned cone half-angle (°)",
        "Tighter cone used when only UWB is steering (no vision lock). "
        "UWB bearing can be biased ~50° if the tag is rotated, so the dog "
        "spins until UWB says it's almost facing the person before walking "
        "forward — by then vision usually picks up and corrects the bearing.",
    ),
    "k_yaw": (
        0.1, 5.0, 0.05, "Yaw P gain",
        "How hard the dog turns to face the person. Higher = snappier turn, "
        "but can overshoot and wobble. Lower = lazy, slow to face.",
    ),
    "k_dist": (
        0.05, 2.0, 0.05, "Distance P gain",
        "How hard the dog accelerates to reach follow distance. Higher = "
        "quicker reaction, but jerkier. Lower = smoother but lags behind.",
    ),
    "max_yaw_rate": (
        0.1, 2.0, 0.05, "Max yaw rate (rad/s)",
        "Hard cap on how fast the dog can spin. Even a huge bearing error "
        "won't make it spin faster than this.",
    ),
    "max_vx": (
        0.1, 1.5, 0.05, "Max forward speed (m/s)",
        "Hard cap on how fast the dog can walk. No matter how far the "
        "person is, the dog won't go faster than this.",
    ),
    "enter_following_confidence": (
        0.0, 1.0, 0.05, "Enter FOLLOWING confidence",
        "How sure the tracker has to be before the dog starts following. "
        "Higher = waits for a solid lock; safer but slower to start moving.",
    ),
    "exit_following_confidence": (
        0.0, 1.0, 0.05, "Exit FOLLOWING confidence",
        "How shaky the tracker can get before the dog gives up and stops "
        "following. Lower = sticks with the target through bad patches.",
    ),
    "lost_ride_through_ticks": (
        0, 30, 1, "Lost ride-through (ticks)",
        "How many bad samples in a row the dog ignores before it decides "
        "the person is really gone. At the default 10 Hz, 1 tick ≈ 100 ms.",
    ),
    "recovery_timeout_s": (
        0.5, 30.0, 0.5, "Recovery timeout (s)",
        "After losing the person, how long the dog keeps spinning to look "
        "for them before giving up and standing still.",
    ),
    "recovery_yaw_rate": (
        0.0, 1.5, 0.05, "Recovery yaw rate (rad/s)",
        "How fast the dog spins while looking for a lost person. Slower = "
        "more time to spot them; faster = covers more angle quickly.",
    ),
}


class ParamStore:
    """Thread-safe holder for the live `Params`. The FSM snapshots once
    per tick (read-only ref to a frozen dataclass) and the mode-server
    swaps the underlying ref atomically on each /params POST.

    On every `update()` and `reset()` we persist the current params to
    `path` (default `PARAMS_PATH`) so a brain restart can re-load them.
    Pass `path=None` to disable persistence (useful for tests)."""

    def __init__(self, params: Params, path: Optional[Path] = None) -> None:
        self._params = params
        self._lock = threading.Lock()
        # `path` defaults to PARAMS_PATH on construction so tests can pass
        # a temp path or None. Sentinel: explicit None disables persistence.
        self._path: Optional[Path] = path if path is not None else PARAMS_PATH

    @property
    def path(self) -> Optional[Path]:
        """Where overrides are persisted, or None if persistence is off."""
        return self._path

    def snapshot(self) -> Params:
        """Return the current Params. Frozen dataclass → safe to read
        outside the lock."""
        with self._lock:
            return self._params

    def update(self, **changes: Any) -> Params:
        """Validate `changes` against PARAM_BOUNDS and atomically replace.

        Coerces each value to the existing field's Python type so JSON
        payloads with lost_ride_through_ticks=5.0 don't quietly become
        floats. Raises `ValueError` on unknown keys, type errors, or
        out-of-bounds values — the mode-server turns those into HTTP 400.
        On success, persists the new params to disk."""
        coerced: dict[str, Any] = {}
        with self._lock:
            current = self._params
            for name, raw in changes.items():
                if name not in PARAM_BOUNDS:
                    raise ValueError(f"unknown param {name!r}")
                existing = getattr(current, name)
                try:
                    value = type(existing)(raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"{name}: cannot coerce {raw!r} to {type(existing).__name__}: {exc}"
                    ) from exc
                lo, hi, _, _, _ = PARAM_BOUNDS[name]
                if not (lo <= value <= hi):
                    raise ValueError(
                        f"{name}={value} out of bounds [{lo}, {hi}]"
                    )
                coerced[name] = value
            new = dataclasses.replace(current, **coerced)
            self._params = new
            self._save_locked(new)
            logger.info("params updated: %s", coerced)
            return new

    def reset(self) -> Params:
        """Revert to env defaults and clear the persistence file. After
        reset, a brain restart re-reads env vars cleanly — no stale
        overrides linger on disk."""
        with self._lock:
            self._params = Params.from_env()
            self._delete_locked()
            logger.info("params reset to env defaults")
            return self._params

    def _save_locked(self, p: Params) -> None:
        """Atomically write `p` to `self._path`. Caller holds `self._lock`.

        Atomic via temp-file + rename so a crash mid-write can't leave a
        truncated file. Failures are logged and swallowed — we never want
        a disk problem to take down the tick loop."""
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(dataclasses.asdict(p), indent=2))
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning("params: failed to persist to %s: %s", self._path, exc)

    def _delete_locked(self) -> None:
        """Remove the persistence file if it exists. Caller holds the lock."""
        if self._path is None:
            return
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("params: failed to delete %s: %s", self._path, exc)


class State(enum.Enum):
    IDLE = "IDLE"
    SEARCHING = "SEARCHING"
    FOLLOWING = "FOLLOWING"
    RECOVERING = "RECOVERING"


class Action(enum.Enum):
    HOLD = "HOLD"
    SPIN_LEFT = "SPIN_LEFT"
    SPIN_RIGHT = "SPIN_RIGHT"
    ADVANCE = "ADVANCE"
    BACK_OFF = "BACK_OFF"


@dataclass
class Tick:
    state: State
    action: Action     # discrete label, for logs and /go2/brain/intent
    reason: str        # human-readable
    vx: float = 0.0    # m/s, +ve = forward
    vy: float = 0.0    # m/s, +ve = left  (unused in Phase 2; reserved)
    vyaw: float = 0.0  # rad/s, +ve = left


def _label_for(vx: float, vyaw: float) -> Action:
    """Pick a discrete label for a continuous command. vx dominates: if the
    dog is walking, the label is ADVANCE/BACK_OFF regardless of yaw. Pure
    yaw → SPIN_LEFT/RIGHT. Both zero → HOLD."""
    if abs(vx) < 1e-6 and abs(vyaw) < 1e-6:
        return Action.HOLD
    if abs(vx) >= 1e-6:
        return Action.ADVANCE if vx > 0 else Action.BACK_OFF
    return Action.SPIN_LEFT if vyaw > 0 else Action.SPIN_RIGHT


def _compute_velocity(
    bearing_deg: float,
    distance_m: Optional[float],
    p: Params,
    bearing_source: Optional[str] = None,
) -> tuple[float, float]:
    """Return (vx, vyaw) for a target at (bearing_deg, distance_m) under
    params p. If distance is None or bearing exceeds the aligned cone,
    vx is zero — the dog spins in place until it's facing the target.

    The previous "vision_agreement: disagree → vx=0" veto has moved up
    a layer: the brain's TargetFuser now rejects bad bearings as
    outliers before they reach the FSM, so the dog can yaw away from a
    ghost without freezing forward motion (and without inheriting the
    watchtower-side vx clamp).

    When `bearing_source == "uwb"` (UWB-only — no vision lock to cross-
    check the bearing), we use a tighter aligned cone
    (`uwb_only_aligned_half_deg`). Field measurement: rotating the tag
    in place produces ~50° deterministic bearing bias from PDoA, so
    walking forward at the wide cone risks heading off-target. The
    sign of UWB bearing stays correct, so spinning is safe; the dog
    spins until apparent bearing is near zero, by which time the
    operator is inside the camera's ±FOV, vision picks up the matching
    track, and the source flips to "fused"/"vision" — restoring the
    full cone for the final approach. Recovery (trail) and vision/fused
    sources keep using the wide cone."""
    bearing_rad = math.radians(bearing_deg)
    bearing_abs_deg = abs(bearing_deg)

    # Yaw correction. Zero inside dead-band so we don't quiver around 0°.
    # UWB bearing's *sign* is reliable even when its magnitude is biased,
    # so proportional yaw works fine for UWB-only — at worst we overshoot
    # by the bias and the next vision sample corrects us.
    if bearing_abs_deg < p.bearing_dead_band_deg:
        vyaw = 0.0
    else:
        vyaw = max(-p.max_yaw_rate, min(p.max_yaw_rate, p.k_yaw * bearing_rad))

    # Forward command. Only when heading is roughly aligned, AND we have a
    # real distance reading. Distance dead-band keeps the dog from
    # creeping when it's already at follow distance.
    aligned_half = (
        p.uwb_only_aligned_half_deg
        if bearing_source == "uwb"
        else p.aligned_half_deg
    )
    if distance_m is None or bearing_abs_deg > aligned_half:
        vx = 0.0
    else:
        err = distance_m - p.target_distance_m
        if abs(err) < p.distance_dead_band_m:
            vx = 0.0
        else:
            vx = max(-p.max_vx, min(p.max_vx, p.k_dist * err))

    return vx, vyaw


class StateMachine:
    def __init__(self, params_store: Optional[ParamStore] = None) -> None:
        self._params_store = params_store or ParamStore(Params.from_env())
        self._state = State.IDLE
        # Ride-through bookkeeping: count consecutive bad samples while in
        # FOLLOWING, and remember the last command so we can re-emit it.
        self._lost_streak = 0
        self._last_action = Action.HOLD
        self._last_reason = ""
        self._last_vx = 0.0
        self._last_vy = 0.0
        self._last_vyaw = 0.0
        # Active-search bookkeeping. We remember the last bearing we
        # successfully tracked so RECOVERING knows which way to spin.
        self._last_seen_bearing_deg = 0.0
        self._recovering_since: float | None = None

    @property
    def state(self) -> State:
        return self._state

    @property
    def lost_streak(self) -> int:
        return self._lost_streak

    @property
    def params_store(self) -> ParamStore:
        return self._params_store

    @property
    def recover_elapsed_s(self) -> float:
        """Seconds since RECOVERING started, or 0 when not in RECOVERING.

        Read by the main loop *before* calling `step()` so the trail
        helper can produce a body-frame goal at the right phase boundary.
        On the first tick of a fresh RECOVERING entry, this returns 0
        (the FSM hasn't set `_recovering_since` yet); on that same tick
        `_emit_recovering` will internally see elapsed=0 from its own
        bookkeeping, so both code paths agree."""
        if self._recovering_since is None:
            return 0.0
        return time.monotonic() - self._recovering_since

    def step(
        self,
        target: Optional[FusedTarget],
        trail_goal: Optional[TrailGoal] = None,
    ) -> Tick:
        """Advance one tick. Returns the chosen velocity command, label,
        and reason. `trail_goal` is consulted only in RECOVERING; it's
        the FSM's contract that main.py will pass it during recovery."""
        now = time.monotonic()
        # One snapshot per tick so the tick is internally consistent even
        # if a /params POST lands mid-step.
        p = self._params_store.snapshot()

        # ACQUIRING is "I see something but not solidly enough to drive on"
        # — explicitly route it to SEARCHING before the is_bad gate, so the
        # cold-start path (UWB spends ~1 s in ACQUIRING before TRACKING)
        # transitions IDLE → SEARCHING → FOLLOWING. Without this, ACQUIRING
        # has is_followable=False and falls into the bad-sample branch
        # below, leaving the dog stuck in IDLE during cold start.
        if target is not None and target.tracking_state == "ACQUIRING":
            self._lost_streak = 0
            self._recovering_since = None
            return self._enter(
                State.SEARCHING,
                f"acquiring; wait for confidence "
                f"(conf={target.confidence:.2f}, age={target.age_s:.2f}s)",
                vx=0.0, vyaw=0.0,
            )

        # Bad-sample handling. While FOLLOWING, absorb a short burst of
        # missing/un-followable samples by re-emitting the last velocity —
        # the UWB filter routinely drops single samples through the
        # Mahalanobis gate, and stuttering kills follow smoothness. After
        # the burst we drop into RECOVERING, which uses the ghost trail
        # if we have one and falls back to spin-toward-last-bearing if
        # we don't.
        is_bad = target is None or not target.is_followable
        if is_bad:
            if (
                self._state == State.FOLLOWING
                and self._lost_streak < p.lost_ride_through_ticks
            ):
                self._lost_streak += 1
                reason = (
                    f"predicting through lost sample "
                    f"({self._lost_streak}/{p.lost_ride_through_ticks})"
                )
                return Tick(
                    state=State.FOLLOWING,
                    action=self._last_action,
                    reason=reason,
                    vx=self._last_vx,
                    vy=self._last_vy,
                    vyaw=self._last_vyaw,
                )

            # Ride-through exhausted while FOLLOWING → start active search.
            if self._state == State.FOLLOWING:
                self._lost_streak = 0
                self._recovering_since = now
                return self._emit_recovering(now, p, trail_goal)

            # Already RECOVERING — keep playing the trail / spinning until
            # timeout.
            if self._state == State.RECOVERING:
                return self._emit_recovering(now, p, trail_goal)

            # Other states (IDLE, SEARCHING) just hold on bad samples.
            self._lost_streak = 0
            self._recovering_since = None
            reason = (
                "no target yet"
                if target is None
                else f"unfollowable (conf={target.confidence:.2f}, age={target.age_s:.2f}s)"
            )
            return self._enter(State.IDLE, reason, vx=0.0, vyaw=0.0)

        # Good sample arrived — reset ride-through and recovery.
        self._lost_streak = 0
        self._recovering_since = None

        ts = target.tracking_state
        if ts == "ACQUIRING":
            return self._enter(
                State.SEARCHING, "acquiring; wait for confidence", vx=0.0, vyaw=0.0
            )

        # TRACKING or PREDICTING. Apply hysteresis: only commit to FOLLOWING
        # when confidence climbs above ENTER; only fall back when it drops
        # below EXIT (which is below PREDICTING's 0.4 floor, so PREDICTING
        # holds the FOLLOWING state).
        if self._state == State.FOLLOWING:
            if target.confidence < p.exit_following_confidence:
                return self._enter(
                    State.SEARCHING,
                    f"confidence {target.confidence:.2f} below exit "
                    f"{p.exit_following_confidence}",
                    vx=0.0, vyaw=0.0,
                )
        else:
            if target.confidence < p.enter_following_confidence:
                return self._enter(
                    State.SEARCHING,
                    f"confidence {target.confidence:.2f} below enter "
                    f"{p.enter_following_confidence}",
                    vx=0.0, vyaw=0.0,
                )

        vx, vyaw = _compute_velocity(
            target.bearing_deg, target.distance_m, p, target.bearing_source
        )
        bearing = target.bearing_deg
        distance_str = f"{target.distance_m:.2f} m"
        # Remember the last bearing — RECOVERING uses this only when no
        # ghost trail is available (fallback to today's blind spin).
        self._last_seen_bearing_deg = bearing

        if abs(vx) < 1e-6 and abs(vyaw) < 1e-6:
            reason = (
                f"target centered ({bearing:+.1f}°) at {distance_str} "
                f"[{target.bearing_source}] → hold"
            )
        elif abs(vx) < 1e-6:
            side = "left" if vyaw > 0 else "right"
            reason = (
                f"target {bearing:+.1f}° ({side}) [{target.bearing_source}]; "
                f"recenter (vyaw={vyaw:+.2f})"
            )
        else:
            verb = "advance" if vx > 0 else "back off"
            reason = (
                f"target {bearing:+.1f}° / {distance_str} "
                f"[{target.bearing_source}] → {verb} "
                f"(vx={vx:+.2f}, vyaw={vyaw:+.2f})"
            )

        return self._enter(State.FOLLOWING, reason, vx=vx, vyaw=vyaw)

    def _emit_recovering(
        self,
        now: float,
        p: Params,
        trail_goal: Optional[TrailGoal],
    ) -> Tick:
        """Recovery tick. With a trail goal, drive toward the operator's
        last observed positions. Without one, fall back to today's
        spin-toward-last-bearing. Returns IDLE on timeout or when the
        trail has been fully exhausted (Phase B over)."""
        elapsed = 0.0 if self._recovering_since is None else now - self._recovering_since
        if elapsed > p.recovery_timeout_s:
            self._recovering_since = None
            return self._enter(
                State.IDLE,
                f"recovery timed out after {p.recovery_timeout_s:.1f}s — give up",
                vx=0.0, vyaw=0.0,
            )

        if trail_goal is not None:
            # Drive toward the trail point as if it were a normal target.
            distance_m = math.hypot(trail_goal.body_x, trail_goal.body_y)
            bearing_deg = math.degrees(
                math.atan2(trail_goal.body_y, trail_goal.body_x)
            )
            vx, vyaw = _compute_velocity(bearing_deg, distance_m, p)
            phase = "extrapolate" if trail_goal.extrapolated else "approach"
            reason = (
                f"trail {phase} → ({trail_goal.body_x:+.2f}, "
                f"{trail_goal.body_y:+.2f}) m, bearing {bearing_deg:+.1f}° "
                f"[{elapsed:.1f}/{p.recovery_timeout_s:.1f}s]"
            )
            return self._enter(State.RECOVERING, reason, vx=vx, vyaw=vyaw)

        # Fallback: ghost trail unavailable (no pose stream, or trail
        # exhausted). Spin toward last-seen bearing — same as Phase 1.
        sign = 1.0 if self._last_seen_bearing_deg >= 0 else -1.0
        vyaw = sign * p.recovery_yaw_rate
        side = "left" if vyaw > 0 else "right"
        reason = (
            f"no trail; searching {side} toward last bearing "
            f"{self._last_seen_bearing_deg:+.1f}° "
            f"({elapsed:.1f}/{p.recovery_timeout_s:.1f}s)"
        )
        return self._enter(State.RECOVERING, reason, vx=0.0, vyaw=vyaw)

    def _enter(
        self,
        new_state: State,
        reason: str,
        vx: float = 0.0,
        vy: float = 0.0,
        vyaw: float = 0.0,
    ) -> Tick:
        if new_state != self._state:
            logger.info("FSM %s→%s reason=%r", self._state.value, new_state.value, reason)
            self._state = new_state
        action = _label_for(vx, vyaw)
        self._last_action = action
        self._last_reason = reason
        self._last_vx = vx
        self._last_vy = vy
        self._last_vyaw = vyaw
        return Tick(state=new_state, action=action, reason=reason, vx=vx, vy=vy, vyaw=vyaw)
