"""go2-brain entrypoint.

Wires DecisionSubscriber → StateMachine → DogController and runs a tick
loop. Both `mock` and `unitree` controllers are loaded at startup; the
operator picks which is active via either:

    --controller mock|unitree     (startup default)

or, at runtime, via the mode-server HTTP endpoint:

    curl -X POST http://<dog>:3300/mode/unitree
    curl -X POST http://<dog>:3300/mode/mock
    curl -X POST http://<dog>:3300/stop          (panic: drop to mock + stop)

This means you can boot in safe `mock` mode, watch the brain log
sensible decisions, then flip to `unitree` to actually move the dog —
all without restarting.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

import httpx
from rich.logging import RichHandler

from .dog.mock import MockController
from .dog.switchable import SwitchableController
from .dog.unitree import UnitreeController
from .fusion import TargetFuser
from .intent_publisher import IntentPublisher
from .mode_server import serve_in_thread
from .perception import (
    DecisionSubscriber,
    FreeSpaceSubscriber,
    PoseSubscriber,
    VisionTracksSubscriber,
)
from .safety import SafetyController
from .state_machine import Params, ParamStore, State, StateMachine
from .trail import GhostTrail

TICK_HZ = float(os.environ.get("BRAIN_TICK_HZ", "10"))
LOG_LEVEL = os.environ.get("BRAIN_LOG_LEVEL", "INFO").upper()
HEARTBEAT_HZ = float(os.environ.get("BRAIN_HEARTBEAT_HZ", "1"))
MODE_PORT = int(os.environ.get("BRAIN_MODE_PORT", "3300"))
MOTION_BASE_URL = os.environ.get("MOTION_BASE_URL", "http://127.0.0.1:3201")
READY_MOTION_TIMEOUT_S = float(os.environ.get("BRAIN_READY_MOTION_TIMEOUT_S", "10"))
READY_DECISION_TIMEOUT_S = float(os.environ.get("BRAIN_READY_DECISION_TIMEOUT_S", "10"))
# Give the dog room to finish its boot dance before the tick loop floods
# motion with /velocity commands and cancels it.
STARTUP_DANCE_SETTLE_S = float(os.environ.get("BRAIN_STARTUP_DANCE_SETTLE_S", "6"))


def _wait_for_motion(logger: logging.Logger, deadline_s: float) -> bool:
    start = time.monotonic()
    last_err: str | None = None
    with httpx.Client(base_url=MOTION_BASE_URL, timeout=1.0) as client:
        while time.monotonic() - start < deadline_s:
            try:
                r = client.get("/health")
                r.raise_for_status()
                logger.info("READY motion=ok url=%s health=%s", MOTION_BASE_URL, r.json())
                return True
            except Exception as exc:
                last_err = str(exc)
                time.sleep(0.5)
    logger.warning(
        "READY motion=fail url=%s deadline=%.1fs err=%s",
        MOTION_BASE_URL, deadline_s, last_err,
    )
    return False


def _wait_for_watchtower(
    logger: logging.Logger, perception: DecisionSubscriber, deadline_s: float
) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < deadline_s:
        d = perception.latest()
        if d is not None:
            logger.info(
                "READY watchtower=ok state=%s conf=%.2f",
                d.tracking_state, d.confidence,
            )
            return True
        time.sleep(0.1)
    logger.warning(
        "READY watchtower=fail topic=rt/go2/uwb/decision deadline=%.1fs",
        deadline_s,
    )
    return False


def _startup_dance(logger: logging.Logger) -> None:
    try:
        with httpx.Client(base_url=MOTION_BASE_URL, timeout=2.0) as client:
            r = client.post("/dance")
            r.raise_for_status()
        logger.info("dance triggered, settling %.1fs before tick loop",
                    STARTUP_DANCE_SETTLE_S)
        time.sleep(STARTUP_DANCE_SETTLE_S)
    except Exception as exc:
        logger.warning("dance request failed: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Go2 brain")
    parser.add_argument(
        "--controller",
        default=os.environ.get("BRAIN_CONTROLLER", "mock"),
        help="Initial controller: 'mock' (default; logs only) or 'unitree' "
        "(real dog motion via go2-motion HTTP API). Either way, the other "
        "controller is loaded too and you can flip via "
        "`curl -X POST http://<dog>:3300/mode/<name>`.",
    )
    parser.add_argument(
        "--domain",
        type=int,
        default=int(os.environ.get("DDS_DOMAIN", "0")),
        help="CycloneDDS domain id. Match this with go2-sim and watchtower.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(name)-25s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False, markup=False)],
    )
    logger = logging.getLogger("brain")

    logger.info(
        "starting initial=%s domain=%d tick=%.1fHz mode-port=%d",
        args.controller, args.domain, TICK_HZ, MODE_PORT,
    )

    # Load BOTH controllers up front. UnitreeController.__init__ doesn't
    # contact go2-motion — it just opens an HTTP client — so it's safe to
    # instantiate even if motion isn't running yet (calls will fail and
    # log when they fire).
    controllers = {
        "mock": MockController(),
        "unitree": UnitreeController(),
    }
    if args.controller not in controllers:
        raise ValueError(
            f"unknown controller {args.controller!r}; expected one of {list(controllers)}"
        )
    controller = SwitchableController(controllers, default=args.controller)

    # Tunables live on a single ParamStore shared between the FSM (which
    # snapshots once per tick) and the mode server (which mutates on
    # /params POST). Initial values come from BRAIN_* env vars, then any
    # persisted overrides (BRAIN_PARAMS_PATH, default ~/.go2-brain/params.json)
    # overlay on top. The store re-saves on every update so a restart picks
    # up exactly where the operator left off.
    param_store = ParamStore(Params.load())
    if param_store.path is not None:
        logger.info("params persistence: %s", param_store.path)

    # Mode-switch + live-tuning HTTP endpoint (UI on GET /, params on /params)
    # The mode server targets the SwitchableController directly so /mode/<name>
    # and /stop bypass the safety wrapper — both are operator commands that
    # must never be silently clipped.
    mode_server = serve_in_thread(controller, param_store, port=MODE_PORT)

    perception = DecisionSubscriber(domain=args.domain)
    # Vision tracks, dog pose, and free_space are *optional* perception
    # inputs. If they never publish (e.g., running against today's go2-sim,
    # or watchtower without the new JSON publishers), fusion falls back
    # to UWB-only, the ghost trail stays empty, and the safety wrapper
    # passes velocities through unchanged (unless BRAIN_SAFETY_STRICT=1).
    vision = VisionTracksSubscriber(domain=args.domain)
    pose = PoseSubscriber(domain=args.domain)
    free_space = FreeSpaceSubscriber(domain=args.domain)
    # Safety wrapper sits between the FSM and the underlying controller.
    # Yaw is always passed through; vx/vy are clipped when LIDAR sees an
    # obstacle in the direction of travel. The mode server keeps a direct
    # reference to `controller` (the SwitchableController) so /mode/<name>
    # still flips the inner controller; the tick loop sends through `safe`.
    safe = SafetyController(controller, free_space)
    machine = StateMachine(param_store)
    fuser = TargetFuser()
    trail = GhostTrail()
    intent = IntentPublisher(domain=args.domain)

    perception.start()
    vision.start()
    pose.start()
    free_space.start()
    stop = False

    def _on_signal(signum: int, _frame) -> None:
        nonlocal stop
        logger.info("signal %d received; shutting down", signum)
        stop = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # Readiness handshake: confirm motion + watchtower are talking to us
    # before the tick loop starts driving the dog. If we're already in
    # unitree mode and both are healthy, do a celebratory dance — the
    # operator can see at a glance that the full chain is wired up.
    motion_ok = _wait_for_motion(logger, READY_MOTION_TIMEOUT_S)
    watchtower_ok = _wait_for_watchtower(logger, perception, READY_DECISION_TIMEOUT_S)
    # Vision + pose + free_space are best-effort — log their presence but
    # don't block startup.
    vision_ok = vision.latest() is not None
    pose_ok = pose.latest() is not None
    free_space_ok = free_space.latest() is not None
    logger.info(
        "perception channels: uwb=%s vision=%s pose=%s free_space=%s "
        "(vision/pose/free_space are optional; missing → degraded fallback)",
        "ok" if watchtower_ok else "missing",
        "ok" if vision_ok else "missing",
        "ok" if pose_ok else "missing",
        "ok" if free_space_ok else "missing",
    )
    if motion_ok and watchtower_ok:
        logger.info("READY all=ok mode=%s", controller.current_name)
        if controller.current_name == "unitree":
            _startup_dance(logger)
        else:
            logger.info(
                "dance skipped (mode=%s); POST :%d/mode/unitree to enable motion",
                controller.current_name, MODE_PORT,
            )
    else:
        logger.warning(
            "READY degraded motion=%s watchtower=%s",
            motion_ok, watchtower_ok,
        )

    period_s = 1.0 / TICK_HZ
    # Heartbeat: emit a TICK line every N ticks (default 1 Hz) showing the
    # live FSM state, last decision, and commanded velocity. This is the
    # main "what is the dog doing right now" log — between FSM transitions
    # the brain is otherwise silent. State transitions themselves are
    # logged immediately by StateMachine._enter (FSM A→B).
    heartbeat_period = max(1, int(round(TICK_HZ / max(HEARTBEAT_HZ, 0.001))))
    tick_n = 0
    # Cap how often we log per-tick exceptions so a sticky failure doesn't
    # firehose the log. After this many consecutive failures we give up
    # and exit so the container restart actually happens (something's
    # genuinely broken, not just a transient bad sample).
    consecutive_tick_errors = 0
    MAX_CONSECUTIVE_TICK_ERRORS = 50  # ~5 s at 10 Hz
    try:
        while not stop:
            try:
                tick_start = time.monotonic()
                decision = perception.latest()
                decision_age = perception.age_s()
                tracks = vision.latest()
                tracks_age = vision.age_s()
                current_pose = pose.latest()

                target = fuser.fuse(
                    decision, decision_age, tracks, tracks_age
                )

                # Log the operator's path while we have a healthy lock so
                # RECOVERING can replay it. Skipping in non-FOLLOWING avoids
                # logging stale or noisy positions during ACQUIRING/IDLE.
                if (
                    machine.state == State.FOLLOWING
                    and target is not None
                    and target.is_followable
                ):
                    trail.append(target, current_pose)

                # Compute the recovery goal *before* step() so the FSM can
                # use it on the same tick FOLLOWING flips to RECOVERING.
                # `recover_elapsed_s` returns 0 when not yet RECOVERING, which
                # is exactly Phase A's start-of-recovery semantics.
                trail_goal = trail.goal_for_recovery(
                    current_pose, machine.recover_elapsed_s
                )

                tick = machine.step(target, trail_goal)

                # Reset the trail when we abandon the recovery — IDLE means
                # we've decided the operator is gone, so old breadcrumbs
                # would mislead the next acquisition cycle.
                if tick.state == State.IDLE and len(trail) > 0:
                    trail.reset()

                # Send the tick's velocity through the safety wrapper. This is
                # what may clip vx/vy when LIDAR sees an obstacle ahead. Yaw
                # is unchanged. The wrapper updates its own status snapshot,
                # which we read for telemetry below.
                safe.set_velocity(vx=tick.vx, vy=tick.vy, vyaw=tick.vyaw)
                safety_status = safe.status()

                if tick_n % heartbeat_period == 0:
                    if target is None:
                        tgt_str = "target=none"
                    else:
                        tgt_str = (
                            f"target={target.tracking_state} "
                            f"src={target.bearing_source} "
                            f"conf={target.confidence:.2f} "
                            f"dist={target.distance_m:.2f}m "
                            f"bearing={target.bearing_deg:+.1f}"
                        )
                    vis_str = (
                        f"vid={target.vision_track_id}"
                        if target is not None and target.vision_track_id
                        else "vid=-"
                    )
                    # Surface clip activity in the heartbeat so operators see
                    # the wrapper kicking in without reading the intent JSON.
                    if safety_status.clipped:
                        safe_str = (
                            f"safe=CLIP ahead={safety_status.min_ahead_m:.2f}m "
                            f"sx={safety_status.scale_vx:.2f} sy={safety_status.scale_vy:.2f}"
                            if safety_status.min_ahead_m is not None
                            else "safe=CLIP(strict-blocked)"
                        )
                    elif safety_status.min_ahead_m is not None:
                        safe_str = f"safe=ok ahead={safety_status.min_ahead_m:.2f}m"
                    else:
                        safe_str = "safe=ok ahead=?"
                    logger.info(
                        "TICK state=%s action=%s mode=%s vx=%+.2f vyaw=%+.2f "
                        "%s %s %s trail=%d lost=%d reason=%r",
                        tick.state.value, tick.action.value, controller.current_name,
                        tick.vx, tick.vyaw, tgt_str, vis_str, safe_str, len(trail),
                        machine.lost_streak, tick.reason,
                    )
                tick_n += 1

                intent.publish(
                    tick=tick,
                    mode=controller.current_name,
                    lost_streak=machine.lost_streak,
                    decision=decision,
                    fused=target,
                    trail_len=len(trail),
                    safety=safety_status,
                )

                elapsed = time.monotonic() - tick_start
                time.sleep(max(0.0, period_s - elapsed))
                consecutive_tick_errors = 0
            except Exception:
                consecutive_tick_errors += 1
                # Log first occurrence + every 10th to avoid spam, but
                # always log with traceback so we can fix the root cause.
                if consecutive_tick_errors == 1 or consecutive_tick_errors % 10 == 0:
                    logger.exception(
                        "tick failure #%d (continuing — UI stays up)",
                        consecutive_tick_errors,
                    )
                if consecutive_tick_errors >= MAX_CONSECUTIVE_TICK_ERRORS:
                    logger.error(
                        "tick failed %d times in a row; exiting so the "
                        "container restart actually surfaces the problem",
                        consecutive_tick_errors,
                    )
                    raise
                # Backoff so we don't spin tight on a sticky failure.
                time.sleep(period_s)
    finally:
        # safe.stop() routes to the inner controller's stop() — same
        # effect as controller.stop() but goes through the wrapper for
        # consistency. (The wrapper never blocks stop.)
        safe.stop()
        perception.stop()
        vision.stop()
        pose.stop()
        free_space.stop()
        try:
            mode_server.shutdown()
        except Exception:
            pass
        try:
            safe.close()
        except Exception:
            logger.exception("error closing controllers")
        logger.info("brain stopped")


if __name__ == "__main__":
    sys.exit(main() or 0)
