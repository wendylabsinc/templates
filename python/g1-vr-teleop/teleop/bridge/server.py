#!/usr/bin/env python3
"""G1 VR Teleop bridge.

Serves the WebXR page and a WebSocket endpoint. Receives per-frame hand/head
data from the Quest, retargets it to G1 joint targets, and pushes them to a
pluggable SimSink.

Two orthogonal choices:
  --retarget debug|g1   how frames become joint targets
  --sink     log|sim    where joint targets go

Defaults (`debug` + `log`) have NO heavy deps and run today: open the page on
the Quest and watch hand data land in the terminal, before any sim or robot.

    python3 -m pip install aiohttp                 # for the default path
    python3 -m pip install -r requirements.txt     # + dex-retargeting/pinocchio for --retarget g1
    python3 server.py                              # debug retarget, log sink
    python3 server.py --retarget g1 --sink sim     # full G1 path (needs deps + sim up)
"""

import argparse
import asyncio
import hashlib
import json
import math
import ssl
import io
import tarfile
import os
import secrets
import time

from aiohttp import web, WSMsgType

from session_recorder import SessionRecorder
from retarget_g1 import ARM_LEFT, ARM_RIGHT

WEB_DIR = __file__.rsplit("/", 2)[0] + "/web"


# --------------------------------------------------------------------------- #
# Retargeters: frame -> {joint_name: target_rad}
# --------------------------------------------------------------------------- #
class DebugRetargeter:
    """No deps. Summarizes each frame so you can confirm the data path."""
    def process(self, frame: dict) -> dict:
        out = {}
        for h in frame.get("hands", []):
            side = h.get("hand", "?")
            lm = h.get("landmarks") or []
            n = len(lm) // 3 if lm and isinstance(lm[0], (int, float)) else len(lm)
            out[f"{side}_landmarks"] = float(n)
            w = h.get("wrist")
            if w:
                out[f"{side}_wrist_x"] = float(w[0])
        return out


def make_retargeter(kind: str):
    if kind == "g1":
        from retarget_g1 import G1Retargeter   # heavy: dex_retargeting + pinocchio
        return G1Retargeter()
    return DebugRetargeter()


# --------------------------------------------------------------------------- #
# The arm math: one switch, TELEOP_ARM.
#
#   solver    Unitree's vendored Pinocchio/CasADi IK. All fourteen joints from
#             both wrist poses, one solve per frame on a worker thread so the
#             fingers keep headset rate; the arm trails by up to one solve
#             (~95-200 ms). The baseline: what the stack this repo grew out of
#             does with the same hand data.
#   geometry  The closed form built here: elbow from reach, shoulder pitch and
#             roll from direction with yaw only when roll cannot reach inside
#             the robot's limits, wrist from orientation against the forearm.
#             Microseconds per frame, and every one of its assumptions was
#             found on the robot one at a time.
#   fingers   Hands only; no arm joint goes on the wire.
#
# Until this existed choosing meant knowing five environment variables, and
# the deploy script hard-coded the geometry. A comparison of the two paths --
# same headset, same bridge, same onboard, only the arm math differs -- needs
# the choice to be one word, printed in the banner, reported by
# /control/status and written into every recording's header, so an episode
# cannot be misread as the other path's.
#
# Without TELEOP_ARM the old flags decide exactly as before, so nothing
# changes underneath a deploy that never heard of the switch.
# --------------------------------------------------------------------------- #
ARM_MODES = ("solver", "geometry", "fingers")


def _flag(env, name, default):
    v = env.get(name, "")
    if v == "":
        return default
    return v not in ("0", "off", "no", "false")


def arm_mode_from_env(env=None) -> dict:
    """-> {mode, forearm, shoulder, shoulder_yaw, wrist_fk, fingers_only, async_ik}."""
    env = os.environ if env is None else env
    mode = env.get("TELEOP_ARM", "").strip().lower()
    if mode and mode not in ARM_MODES:
        raise ValueError(f"TELEOP_ARM={mode!r}; expected one of {', '.join(ARM_MODES)}")
    if not mode:
        # The pre-switch rules, verbatim: FOREARM wins, then FINGERS_ONLY,
        # else the inline IK.
        if _flag(env, "TELEOP_FOREARM", False):
            mode = "geometry"
        elif _flag(env, "TELEOP_FINGERS_ONLY", False):
            mode = "fingers"
        else:
            mode = "solver"
        explicit = False
    else:
        explicit = True
    cfg = {"mode": mode, "forearm": False, "shoulder": False, "shoulder_yaw": False,
           "wrist_fk": False, "fingers_only": False, "async_ik": False}
    if mode == "geometry":
        cfg["forearm"] = True
        # Explicit geometry defaults the shoulder ON: that is the path the
        # hardware runs now use. The legacy spelling keeps its opt-in.
        cfg["shoulder"] = _flag(env, "TELEOP_SHOULDER", explicit)
        if cfg["shoulder"]:
            cfg["shoulder_yaw"] = _flag(env, "TELEOP_SHOULDER_YAW", True)
            cfg["wrist_fk"] = _flag(env, "TELEOP_WRIST_FK", True)
    elif mode == "fingers":
        cfg["fingers_only"] = True
    else:
        # Explicit solver defaults the worker ON; the inline solve set the
        # rate for everything (85 fps in, 10.6 out). Legacy keeps its opt-in.
        cfg["async_ik"] = _flag(env, "TELEOP_ASYNC_IK", explicit)
    return cfg


def prepare_env_for_arm_mode(cfg: dict, env=None) -> None:
    """G1Retargeter reads TELEOP_ASYNC_IK in its constructor: write it first."""
    env = os.environ if env is None else env
    env["TELEOP_ASYNC_IK"] = "1" if cfg["async_ik"] else "0"


def apply_arm_mode(retargeter, cfg: dict):
    """Set the mode flags on a built retargeter. Returns it."""
    retargeter.forearm = cfg["forearm"]
    retargeter.shoulder = cfg["shoulder"]
    retargeter.shoulder_yaw = cfg["shoulder_yaw"]
    retargeter.wrist_fk = cfg["wrist_fk"]
    retargeter.fingers_only = cfg["fingers_only"]
    return retargeter


def smoothing_from_env(env=None) -> dict:
    """Parse the bridge output-filter configuration.

    The retargeter stays a passthrough when used directly by replay/tests.
    A real deployment opts in with TELEOP_SMOOTH_MODE, which makes the choice
    explicit in the process environment and therefore reproducible in the
    session header.
    """
    env = os.environ if env is None else env
    mode = str(env.get("TELEOP_SMOOTH_MODE", "off") or "off").strip().lower()
    if mode not in ("oneeuro", "ema", "off"):
        raise ValueError("TELEOP_SMOOTH_MODE must be oneeuro, ema or off")

    def number(name, default, lo, hi):
        try:
            value = float(env.get(name, default))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a number") from exc
        if not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"{name} must be finite in [{lo}, {hi}]")
        return value

    return {
        "mode": mode,
        "min_cutoff": number("TELEOP_SMOOTH_MIN_CUTOFF", 1.0, 0.01, 20.0),
        "beta": number("TELEOP_SMOOTH_BETA", 1.0, 0.0, 20.0),
        "tau": number("TELEOP_SMOOTH_TAU", 0.0, 0.0, 1.0),
        "joint_cost": {
            "shoulder": number("TELEOP_SMOOTH_SHOULDER_COST", 1.0, 0.1, 10.0),
            "elbow": number("TELEOP_SMOOTH_ELBOW_COST", 1.0, 0.1, 10.0),
            "wrist": number("TELEOP_SMOOTH_WRIST_COST", 1.0, 0.1, 10.0),
        },
    }


def apply_smoothing(retargeter, cfg: dict):
    """Apply a parsed filter config when this retargeter supports it."""
    if not hasattr(retargeter, "set_smooth"):
        return retargeter
    retargeter.set_smooth_tau(cfg["tau"])
    retargeter.set_smooth(mode=cfg["mode"], min_cutoff=cfg["min_cutoff"],
                          beta=cfg["beta"])
    retargeter.set_smooth_cost(**cfg["joint_cost"])
    return retargeter


# --------------------------------------------------------------------------- #
# Sinks: consume {joint_name: target_rad}
# --------------------------------------------------------------------------- #
class SimSink:
    async def apply(self, targets: dict) -> None:  # pragma: no cover
        raise NotImplementedError


class LogSink(SimSink):
    """Prints throughput + a sample of targets. Runs with no robot."""
    def __init__(self):
        self.n = 0
        self.t0 = time.monotonic()

    async def apply(self, targets: dict) -> None:
        self.n += 1
        dt = time.monotonic() - self.t0
        if dt >= 1.0:
            sample = ", ".join(f"{k}={v:+.2f}" for k, v in list(targets.items())[:4])
            print(f"[bridge] {self.n / dt:5.1f} Hz | {len(targets):2d} targets | {sample}")
            self.n = 0
            self.t0 = time.monotonic()


class DirectSimSink(SimSink):
    """STUB: talk directly to the wendy-sim backend (gRPC/WS on :51251).

    The per-frame path must bypass MCP for latency. Fill this in once the sim is
    running and we've captured the wire format for a joint-position command.
    Keep a single persistent connection here; make writes latest-wins.
    """
    def __init__(self, world_id: str, robot_id: str | None = None):
        self.world_id = world_id
        self.robot_id = robot_id
        # TODO: open a persistent gRPC/WS channel to 127.0.0.1:51251

    async def apply(self, targets: dict) -> None:
        # TODO: send a joint-position-target command for `targets`.
        raise NotImplementedError("wire up the sim channel — see README step 3")


_ARM_HINTS = ("shoulder", "elbow", "wrist")


def _note_targets(stats, targets):
    """Record how much the retargeted ARM targets are actually moving.

    "Armed but not following" has two completely different causes: nothing is
    being commanded, or something is commanded and the robot ignores it. Only
    the commanded values separate them, and the arm transform in retarget_g1
    (_R_XR_TO_ROBOT / _HEAD_TO_TORSO / _REACH_SCALE) has never been calibrated
    -- an IK saturating against an unreachable target looks exactly like a dead
    link from outside.
    """
    if not isinstance(targets, dict):
        return
    # Fingers FIRST, before the arm check can return. Fingers-only mode sends
    # no arm joints at all, and the finger range is the only following signal
    # it has -- this call used to sit below the early return, so exactly that
    # mode was the one that lost it.
    _note_fingers(stats, targets)
    names = sorted(n for n in targets
                   if any(h in n for h in _ARM_HINTS) and "hand" not in n)
    if not names:
        stats["arm_targets"] = 0
        return
    row = [round(float(targets[n]), 4) for n in names]
    h = stats.setdefault("_arm_hist", [])
    h.append(row)
    if len(h) > 300:
        del h[:-300]
    stats["arm_names"] = names
    stats["arm_now"] = row
    travel = [round(max(c) - min(c), 4) for c in zip(*h)]
    stats["arm_travel"] = travel
    stats["arm_moving"] = max(travel) > 0.01
    stats["arm_targets"] = len(names)


def _control_allowed(request) -> bool:
    """Gate the state-changing endpoints on a shared secret.

    The onboard commander has had a key since it existed; the bridge endpoint
    that DRIVES it never did. Anything that can reach this port -- a port
    scanner, a health check, a colleague on the office wifi -- could arm a
    35 kg robot and set it following whatever the headset last sent.

    Off by default so it cannot silently lock an operator out mid-session; set
    TELEOP_CONTROL_KEY and it is required. The startup banner says which.
    """
    secret = request.app.get("control_key") or ""
    if not secret:
        return True
    given = request.query.get("key") or request.headers.get("X-Teleop-Key", "")
    return secrets.compare_digest(given, secret)


def _locked(request):
    """The 403 for a caller without the key, or None when they have it.

    Checked FIRST in every handler that uses it, ahead of the 409s and 404s
    that describe the bridge's mode -- so an anonymous caller learns nothing,
    not even which retargeter is loaded.
    """
    if _control_allowed(request):
        return None
    return web.json_response({"ok": False, "error": "bad key"}, status=403)


# Dex3 joint limits, from the vendored URDFs. Mirrored between hands.
_DEX3 = {
    "left": [("left_hand_thumb_0_joint", -1.047, 1.047),
             ("left_hand_thumb_1_joint", -0.724, 0.920),
             ("left_hand_thumb_2_joint", 0.0, 1.745),
             ("left_hand_middle_0_joint", -1.571, 0.0),
             ("left_hand_middle_1_joint", -1.745, 0.0),
             ("left_hand_index_0_joint", -1.571, 0.0),
             ("left_hand_index_1_joint", -1.745, 0.0)],
    "right": [("right_hand_thumb_0_joint", -1.047, 1.047),
              ("right_hand_thumb_1_joint", -0.920, 0.724),
              ("right_hand_thumb_2_joint", -1.745, 0.0),
              ("right_hand_middle_0_joint", 0.0, 1.571),
              ("right_hand_middle_1_joint", 0.0, 1.745),
              ("right_hand_index_0_joint", 0.0, 1.571),
              ("right_hand_index_1_joint", 0.0, 1.745)],
}


def _note_fingers(stats, targets):
    """How much of each finger joint's range is actually being commanded.

    "The fingers move but not fully" has several causes that need different
    fixes -- DexPilot under-scaling a human hand onto a smaller one, a joint
    clamped at a limit, or one finger simply not tracking. Percentage of range
    used, per joint, separates them.
    """
    h = stats.setdefault("_finger_hist", [])
    row = {}
    for side, joints in _DEX3.items():
        for name, lo, hi in joints:
            if name in targets:
                row[name] = float(targets[name])
    if not row:
        return
    h.append(row)
    if len(h) > 400:
        del h[:-400]
    used = {}
    for side, joints in _DEX3.items():
        for name, lo, hi in joints:
            vals = [r[name] for r in h if name in r]
            if not vals:
                continue
            span = max(vals) - min(vals)
            used[name.replace("_hand_", " ").replace("_joint", "")] = {
                "used_pct": round(100.0 * span / (hi - lo), 1),
                "at_min": round(min(vals), 3), "at_max": round(max(vals), 3),
                "limit": [lo, hi],
            }
    stats["fingers"] = used


async def wristcal_handler(request: web.Request) -> web.Response:
    """Flip a wrist axis sign live, without a redeploy.

    The offline sweep could not settle direction -- its top four candidates
    were sign-flips of each other with identical range. Rather than redeploy
    per guess (each one costs the operator a VR re-entry, which is what made
    the finger hunt expensive), the three signs per hand are flippable while
    armed.
    """
    # Gated like /control/*: flipping a sign reverses which way a real arm
    # moves, which is not a read-only thing to leave open.
    if not _control_allowed(request):
        return web.json_response({"ok": False, "error": "bad key"}, status=403)
    r = request.app["retargeter"]
    if not hasattr(r, "flip_wrist"):
        return web.json_response({"ok": False, "error": "no wrist mapping"}, status=409)
    side, axis = request.match_info["side"], request.match_info["axis"]
    if side == "zero":
        msg = r.zero_wrists()
    else:
        msg = r.flip_wrist(side, axis)
    print(f"[bridge] wristcal: {msg}")
    return web.json_response({"ok": not msg.startswith("refused"), "msg": msg,
                              "signs": r._WRIST_SIGN})


async def elbowcal_handler(request: web.Request) -> web.Response:
    """Capture one end of the operator's reach, per hand.

    Same shape as the finger calibration, and for the same reason: the exact
    law of cosines needs the operator's upper-arm and forearm lengths and their
    shoulder position, none of which WebXR reports. Measuring where this
    operator's reach actually ends is cheaper than estimating three constants
    and hoping.
    """
    if not _control_allowed(request):
        return web.json_response({"ok": False, "error": "bad key"}, status=403)
    r = request.app["retargeter"]
    if not hasattr(r, "elbow_cal"):
        return web.json_response({"ok": False, "error": "no elbow mapping"}, status=409)
    which = request.match_info["which"]
    if which == "auto":
        # Calibrate from a WINDOW, not an instant.
        #
        # Capturing at a moment someone else picks is how the live captures
        # went wrong: fired before the operator had reached, the span came back
        # 3.9 cm and inverted on one hand. Watching for a few seconds while
        # they move through their range takes the timing out of it -- the same
        # record-then-decide move that settled the fingers and the wrists.
        secs = max(3.0, min(60.0, float(request.query.get("secs", "15"))))
        seen = {"left": [], "right": []}
        t0 = time.time()
        while time.time() - t0 < secs:
            for side, w in (request.app["stats"].get("_last_wrists") or {}).items():
                if side in seen and w:
                    seen[side].append(list(w))
            await asyncio.sleep(0.05)
        msgs = []
        for side, pts in seen.items():
            if len(pts) < 20:
                msgs.append(f"refused: only {len(pts)} {side} samples")
                continue
            import numpy as _np
            d = _np.linalg.norm(_np.asarray(pts, float)
                                - r._SHOULDER[side], axis=1)
            # Percentiles, not min/max: one dropout frame at an absurd position
            # would otherwise define the whole range.
            near, far = (float(_np.percentile(d, 3)), float(_np.percentile(d, 97)))
            msgs.append(r.elbow_cal(side, "near", (r._SHOULDER[side]
                                                   + _np.array([0.0, 0.0, -near])).tolist()))
            msgs.append(r.elbow_cal(side, "far", (r._SHOULDER[side]
                                                  + _np.array([0.0, 0.0, -far])).tolist()))
        for m in msgs:
            print(f"[bridge] elbowcal auto: {m}")
        return web.json_response({"ok": True, "msg": "; ".join(msgs),
                                  "cal": {k: list(v) for k, v in r._ELBOW_CAL.items()}})
    last = request.app["stats"].get("_last_wrists") or {}
    if not last:
        return web.json_response({"ok": False, "error": "no hand data"}, status=409)
    msgs = [r.elbow_cal(side, which, w) for side, w in sorted(last.items())]
    for m in msgs:
        print(f"[bridge] elbowcal: {m}")
    return web.json_response({"ok": True, "msg": "; ".join(msgs),
                              "cal": {k: list(v) for k, v in r._ELBOW_CAL.items()}})


async def wristcal_zero_handler(request: web.Request) -> web.Response:
    """Re-capture the wrist reference pose. Do this right before arming."""
    if not _control_allowed(request):
        return web.json_response({"ok": False, "error": "bad key"}, status=403)
    r = request.app["retargeter"]
    if not hasattr(r, "zero_wrists"):
        return web.json_response({"ok": False, "error": "no wrist mapping"}, status=409)
    msg = r.zero_wrists()
    print(f"[bridge] wristcal: {msg}")
    return web.json_response({"ok": True, "msg": msg})


def build_info() -> dict:
    """The commit this bridge was built from, as the deploy told the image.

    The image has no .git, so without this the only record of what code
    produced an episode was the seal's `commit: null`. The deploy reads
    `git rev-parse HEAD` and `git status --porcelain` at build time and passes
    them as G1_TELEOP_COMMIT / G1_TELEOP_DIRTY; the seal (episode_seal._source)
    reads the same two variables, so the header, /control/status and the
    manifest can never disagree about what ran. None means the deploy did not
    say -- never a guess.
    """
    commit = os.environ.get("G1_TELEOP_COMMIT") or None
    flag = os.environ.get("G1_TELEOP_DIRTY", "").strip()
    dirty = {"1": True, "0": False}.get(flag) if commit else None
    return {"commit": commit, "dirty": dirty}


def _sha256_of(path):
    """sha256 of a file's bytes, or None when it cannot be read. Never a guess."""
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _run_config(app) -> dict:
    """Everything set at runtime that changes what the robot is told.

    Read defensively throughout: a missing piece must degrade the header, not
    fail the recording the operator just asked for.
    """
    r = app.get("retargeter")
    sink = app.get("sink")
    cfg = {"source": app.get("source"), "arm_mode": app.get("arm_mode")}
    for name, get in (
            ("shoulder", lambda: bool(getattr(r, "shoulder", False))),
            ("shoulder_yaw", lambda: bool(getattr(r, "shoulder_yaw", False))),
            ("wrist_fk", lambda: bool(getattr(r, "wrist_fk", False))),
            ("forearm", lambda: bool(getattr(r, "forearm", False))),
            ("fingers_only", lambda: bool(getattr(r, "fingers_only", False))),
            ("smoothing", lambda: {"mode": getattr(r, "smooth_mode", None),
                                   "min_cutoff": getattr(r, "smooth_min_cutoff", None),
                                   "beta": getattr(r, "smooth_beta", None),
                                   "tau": getattr(r, "smooth_tau", None),
                                   "joint_cost": dict(getattr(r, "smooth_joint_cost", {}) or {})}),
            ("frozen", lambda: sorted(getattr(r, "frozen", ()) or ())),
            ("wrist_scale", lambda: dict(getattr(r, "wrist_scale", {}) or {})),
            ("waist", lambda: (app["waist"].status() if app.get("waist") is not None else None)),
            ("yaw_max_deg", lambda: round(getattr(r, "YAW_SEARCH_MAX", 0.0) * 57.2957795, 2)),
            ("grip_thumb0", lambda: __import__("retarget_g1").GRIP_THUMB0),
            ("thumb_open", lambda: __import__("retarget_g1").THUMB_OPEN),
            ("trigger_grip", lambda: getattr(r, "_trigger_grip", None).status()
             if getattr(r, "_trigger_grip", None) else None),
            ("human_wrist", lambda: {k: round(float(v), 4)
                                     for k, v in getattr(r, "HUMAN_WRIST", {}).items()}),
            ("elbow_cal", lambda: {k: [round(float(x), 4) for x in v]
                                   for k, v in getattr(r, "_ELBOW_CAL", {}).items()}),
            ("operator_shoulder", lambda: {k: [round(float(x), 4) for x in v]
                                           for k, v in getattr(r, "_SHOULDER", {}).items()}),
            ("finger_cal_joints", lambda: len(getattr(r, "_cal", None) or {})),
            ("gains", lambda: getattr(sink, "onboard_gains", None)),
            # The two files that shape every target, by hash rather than by
            # the word "calibrated": a re-walked limit or a new finger
            # calibration is a different run, and the header must say so.
            # None when the file is absent, never a guess.
            ("arm_cal_sha256", lambda: _sha256_of(
                __import__("retarget_g1").ROBOT_LIMITS_SOURCE)),
            ("finger_cal_sha256", lambda: (_sha256_of(getattr(r, "_CAL_FILE", None))
                                           if getattr(r, "_cal", None) else None)),
            ("onboard_state", lambda: getattr(sink, "onboard_state", None)),
    ):
        try:
            cfg[name] = get()
        except Exception:  # noqa: BLE001 -- a bad field must not cost the recording
            cfg[name] = None
    return cfg


# A squeeze is analogue; this is where it counts as pressed. Well clear of a
# resting finger and well under a deliberate pull.
SQUEEZE_ON = 0.6
SQUEEZE_OFF = 0.4          # hysteresis: a finger hovering at the line must not
                           # toggle the recorder several times a second


def _squeeze_edges(frame, stats, key):
    """-> the sides whose squeeze just crossed from released to pressed.

    Rising edge per side with hysteresis, so one pull is one action however
    long it is held and however much the value trembles at the threshold.
    Hand-tracked frames carry no squeeze and never fire.
    """
    prev = stats.setdefault(key, {})
    fired = []
    for h in frame.get("hands") or ():
        if h.get("source") != "controller":
            continue
        side = h.get("hand")
        v = h.get("squeeze")
        if not isinstance(v, (int, float)):
            continue
        was = prev.get(side, False)
        now = v >= SQUEEZE_ON if not was else v > SQUEEZE_OFF
        if now and not was:
            fired.append(side)
        prev[side] = now
    return fired


def _note_control_event(app, event, **fields):
    """Queue low-rate lifecycle/provenance evidence outside the pose summary."""
    diagnostics = app.get("control_events") if app else None
    if diagnostics is not None:
        diagnostics.emit(event, **fields)
    payload = {"event": event, **fields}
    recorder = app.get("session") if app else None
    if recorder is not None and hasattr(recorder, "note_stream"):
        recorder.note_stream("control.lifecycle", payload)
        return True
    return False


async def _ready_sequence(app, sink):
    """take_control if needed, zero the wrists, arm. The laptop's job, in VR.

    Zeroing before arming is not optional: the wrist joints are solved
    relative to the pose the operator zeroed at, and arming without it makes
    the first commanded wrist whatever the last zero happened to be.
    """
    import asyncio as _a
    state = str(getattr(sink, "onboard_state", ""))
    if state == "STANDBY":
        _note_control_event(app, "control.acquire.requested", command="take_control",
                            source=app.get("source", "xr"))
        sink.command("take_control")
        for _ in range(12):
            await _a.sleep(1.0)
            if str(getattr(sink, "onboard_state", "")).startswith("HOLDING"):
                break
    else:
        _note_control_event(app, "control.acquire.requested", command="arm",
                            source=app.get("source", "xr"))
    r = app.get("retargeter")
    if hasattr(r, "zero_wrists"):
        r.zero_wrists()
        await _a.sleep(1.2)
    if hasattr(r, "prepare_controller_grips"):
        r.prepare_controller_grips(getattr(sink, "last_hand_q", None))
    sink.command("arm")
    _note_control_event(app, "arm.requested", source="quest.both_sticks", phase="awaiting_robot_reply")
    print("[bridge] sticks: wrists zeroed; arm requested, awaiting robot reply", flush=True)
    return True


def arm_on_sticks(frame, stats, app):
    """BOTH thumbstick clicks: arm, or hold and return home when armed.

    A rising edge of the pair is one action. Keep holding either/both sticks
    during home from becoming a new arm request when home completes.
    Returns the ready coroutine for the frame loop to schedule, or None.
    """
    sink = app.get("sink") if app else None
    if sink is None or not hasattr(sink, "command"):
        return None
    down = {h.get("hand") for h in (frame.get("hands") or ())
            if h.get("source") == "controller"
            and (h.get("buttons") or {}).get("stick")}
    inputs = {"left_stick":"left" in down, "right_stick":"right" in down}
    for side in ("left", "right"):
        h = next((h for h in (frame.get("hands") or ()) if h.get("hand") == side), {})
        squeeze = h.get("squeeze")
        inputs[side+"_grip"] = isinstance(squeeze, (int,float)) and squeeze > .5
    signature = tuple(inputs.values())
    if signature != stats.get("_debug_buttons"):
        stats["_debug_buttons"] = signature
        _note_control_event(app, "controller.buttons", **inputs)
    both = {"left", "right"} <= down
    prev = stats.get("_sticks_prev", False)
    stats["_sticks_prev"] = both
    if both != prev:
        _note_control_event(app, "controller.both_sticks", left_stick="left" in down,
                            right_stick="right" in down, state="pressed" if both else "released")
    if not both or prev:
        return None
    if getattr(sink, "onboard_homing", False):
        return None
    if str(getattr(sink, "onboard_state", "")).startswith("ARMED"):
        send_home(sink)
        print("[bridge] sticks: hold then home")
        return None
    return _ready_sequence(app, sink)


# Held, not clicked, and alone. Long enough that it cannot be a slip while
# reaching for the arming press; short enough to do while holding a can.
RELEASE_HOLD_S = 1.5


def release_on_stick_hold(frame, stats, app, now=None):
    """RIGHT stick held ALONE: hand the arms back to the Unitree controller.

    The operator could arm, home, record and mark an outcome from inside the
    headset, but not give the robot back -- and taking the headset off to run
    `relax` ends the XR session, which is the same trap arming was in before
    arm_on_sticks existed.

    `relax` is the real release, not a stop: it freezes the arms, relaxes the
    hands and SHUTS DOWN the arm_sdk publisher. While that publisher runs it
    asserts control at full weight and the robot's own controller has to fight
    it, so "the remote does nothing" is what a still-running bridge looks like
    from the operator's side.

    HELD, not clicked. Every other control here fires on an edge; this one
    ends the session's control of the robot, and a 1.5 s hold is the
    difference between a decision and a thumb resting on a stick.

    ALONE. Both sticks together is arming, and a two-stick press where the
    right lands first and the left follows late would otherwise release
    instead of arm. The left stick at any point during the hold cancels --
    the same rule the miniature's show/hide uses on the other side.

    Releasing is the SAFE direction (STANDBY, nothing commanded), so a
    mistaken one costs a take_control, not an incident.
    """
    sink = app.get("sink") if app else None
    if sink is None or not hasattr(sink, "command"):
        return False
    now = time.monotonic() if now is None else now
    down = {h.get("hand"): bool((h.get("buttons") or {}).get("stick"))
            for h in (frame.get("hands") or ()) if h.get("source") == "controller"}
    right, left = down.get("right", False), down.get("left", False)
    st = stats.get("_release_hold") or {}
    if not right:
        stats["_release_hold"] = {}
        return False
    if left or st.get("cancelled"):
        stats["_release_hold"] = dict(st, t=st.get("t", now), cancelled=True)
        return False
    # `is None`, not falsy: a clock reading of exactly 0.0 is a valid start
    # time, and `not 0.0` restarted the countdown on every frame so the hold
    # could never complete. time.monotonic() is never 0 on a live machine,
    # which is precisely why this would have survived into the headset.
    if st.get("t") is None:
        stats["_release_hold"] = {"t": now, "fired": False, "cancelled": False}
        return False
    if st.get("fired") or now - st["t"] < RELEASE_HOLD_S:
        return False
    stats["_release_hold"] = dict(st, fired=True)
    _note_control_event(app, "control.release.requested", command="relax",
                        source="quest.right_stick_hold")
    sink.command("relax")
    print(f"[bridge] right stick held {RELEASE_HOLD_S}s: relax -- arms handed back "
          "to the robot's own controller (take_control to get them again)")
    return True


def record_on_squeeze(frame, stats, app, name="squeeze"):
    """LEFT squeeze toggles the recording. -> the scheduled task, or None.

    The operator asked for a button to start and stop recording without
    leaving VR: reaching for a laptop between attempts is how a session ends
    up as one long file with four attempts in it and no boundaries.

    A toggle rather than two buttons because there is one spare control per
    hand and the other is wanted for home. Which way it went is never guessed
    -- `recorder.recording` is asked, so a session stopped from the control
    page leaves the next press doing the right thing.

    Goes through start_session/stop_session, NOT recorder.start/stop: those
    two carry the header meta and the check-and-seal, and a session begun or
    ended without them is an episode with no configuration and no manifest
    that looks exactly like a legitimate older one.

    Returns the COROUTINE for the caller to schedule, and does not await it:
    stopping hashes the whole file and every camera frame, and awaiting that
    in the frame loop would stall it for seconds -- the onboard's liveness
    watchdog freezes the arms after one. Returning it rather than scheduling
    it here also keeps this function free of any assumption about a running
    loop, which is what makes it testable.
    """
    recorder = app.get("session") if app else None
    if recorder is None:
        return None
    if "left" not in _squeeze_edges(frame, stats, "_squeeze_rec_prev"):
        return None
    running = bool(getattr(recorder, "recording", False))
    print(f"[bridge] squeeze: {'stopping' if running else 'starting'} session")
    return stop_session(app) if running else start_session(app, name)


def locomotion_on_thumbstick(frame, app):
    """Queue only live XR input; other sources cannot inherit a walk."""
    sink = app.get("sink")
    if not hasattr(sink, "set_locomotion"):
        return
    from locomotion import left_stick_velocity
    permission = walking_permission(app)
    if (app.get("source", "xr") != "xr"
            or getattr(sink, "onboard_state", "") != "ARMED"):
        permission.reset()
        sink.set_locomotion({"active": False, "vx": 0., "yaw": 0.})
        return
    sink.set_locomotion(permission.filter(left_stick_velocity(frame)))


def walking_permission(app):
    from locomotion import WalkingPermission
    if "locomotion_permission" not in app:
        app["locomotion_permission"] = WalkingPermission()
    return app["locomotion_permission"]


async def locomotion_permission_handler(request):
    """The operator can block walking independently of arms and waist."""
    permission = walking_permission(request.app)
    if request.method == "GET":
        return web.json_response({"ok": True, **permission.status()})
    if (refused := _locked(request)) is not None:
        return refused
    raw = request.query.get("allowed", "").strip().lower()
    if raw not in ("0", "1", "false", "true"):
        return web.json_response({"ok": False, "error": "allowed must be 1 or 0"}, status=400)
    permission.set_allowed(raw in ("1", "true"))
    sink = request.app.get("sink")
    if hasattr(sink, "set_locomotion"):
        # Also on enable: purge an old queued velocity before accepting a new
        # neutral frame. No headset frame is needed to request a stop.
        sink.set_locomotion({"active": False, "vx": 0., "yaw": 0.})
    _note_control_event(request.app, "control.locomotion.permission",
                        allowed=permission.allowed, source="operator_ui")
    return web.json_response({"ok": True, **permission.status()})


def waist_on_thumbstick(frame, stats, app, targets, now=None):
    """RIGHT thumbstick x turns the waist. Adds `waist_yaw_joint` to `targets`.

    In the targets dict, by name, like every other joint: that is what puts
    it in the row's `targets`, in `sent` after the sink, and in the episode a
    replay reads -- the waist is part of what the robot was told.

    Runs only while the onboard reports ARMED (the dial forgets itself
    otherwise, so each arming re-seeds from the measured waist), and only for
    a frame that carries a right CONTROLLER: a tracked hand has no stick.
    The measured waist comes from the onboard's body state, motor 12 --
    `body.q[0]`, since BODY_IDX starts at 12.

    With the operator's waist lock on (`app["waist_frozen"]`, set from the
    laptop's control page) the stick reads as CENTRED rather than absent:
    centred sends velocity 0 and holds the target where the onboard has
    actually got to, so a lock applied mid-turn stops the turn. Absent would
    send nothing and leave the last nonzero velocity with the onboard.
    """
    dial = app.get("waist") if app else None
    if dial is None or targets is None:
        return None
    if app.get("waist_frozen"):
        frame = stick_centred(frame)
    sink = app.get("sink")
    armed = str(getattr(sink, "onboard_state", "")).startswith("ARMED")
    measured = None
    body = getattr(sink, "onboard_body", None)
    if isinstance(body, dict):
        idx, q = body.get("idx") or [], body.get("q") or []
        if idx and q and idx[0] == 12 and len(q) == len(idx):
            measured = q[0]
    clock_now = time.monotonic() if now is None else now
    acknowledged = None
    ack_age_s = None
    waist_ack = getattr(sink, "last_waist", None)
    waist_ack_t = getattr(sink, "last_waist_t", 0.0) or 0.0
    if isinstance(waist_ack, dict):
        cmd = waist_ack.get("cmd")
        if isinstance(cmd, (int, float)) and math.isfinite(cmd):
            acknowledged = float(cmd)
            ack_age_s = clock_now - waist_ack_t if waist_ack_t else None
    target = dial.step(frame, armed=armed, measured=measured, now=clock_now,
                       acknowledged=acknowledged, ack_age_s=ack_age_s)
    velocity_fn = getattr(dial, "velocity", None)
    velocity = velocity_fn(frame, armed=armed) if callable(velocity_fn) else None
    if velocity is not None and hasattr(sink, "set_waist_velocity"):
        sink.set_waist_velocity(velocity)
    if target is not None:
        targets["waist_yaw_joint"] = float(target)
    return target


def stick_centred(frame):
    """`frame` with the RIGHT controller's stick x at 0.0; y and the rest kept.

    A copy: the original frame is what the recorder writes, and the row must
    still say what the operator's thumb did while the lock held it.
    """
    hands = []
    for h in (frame.get("hands") or ()):
        if h.get("hand") == "right" and h.get("source") == "controller":
            ts = h.get("thumbstick")
            if isinstance(ts, (list, tuple)) and len(ts) >= 1:
                h = {**h, "thumbstick": [0.0, *ts[1:]]}
        hands.append(h)
    return {**frame, "hands": hands}


async def waist_lock_handler(request: web.Request) -> web.Response:
    """Lock or unlock the waist from the laptop: `?frozen=1` / `?frozen=0`.

    Locked, the right stick no longer turns the waist (waist_on_thumbstick);
    the waist holds where it is and the rest of teleop is unchanged. For a
    demo where the torso should stay facing the table whatever the operator's
    thumb does. GET reports the current setting.
    """
    if request.method == "GET" and "frozen" not in request.query:
        return web.json_response({"ok": True, "frozen": bool(request.app.get("waist_frozen"))})
    if (refused := _locked(request)) is not None:
        return refused
    raw = str(request.query.get("frozen", "")).strip().lower()
    if raw not in ("1", "0", "true", "false"):
        return web.json_response({"ok": False, "error": "frozen must be 1 or 0"}, status=400)
    request.app["waist_frozen"] = raw in ("1", "true")
    print(f"[bridge] waist {'LOCKED by operator' if request.app['waist_frozen'] else 'on the right stick'}")
    return web.json_response({"ok": True, "frozen": request.app["waist_frozen"]})
def home_payload():
    """The closed hand pose the grip would command at a full squeeze.

    Sent WITH every `home`, so the onboard closes the hands the way the grip
    closes them. It used to close them to its own copy of this pose, taken
    when GRIP_THUMB0 was 0: the thumb stayed beside the fingers however the
    live knob was set, which the operator reported as "they dont go full
    close at home". One owner of the pose -- this function -- and the onboard
    keeps its copy only as a fallback for a bridge too old to send one.
    """
    import retarget_g1 as _rg
    return {"hands": {"left": [float(v) for v in _rg.grip_q("left", 1.0)],
                      "right": [float(v) for v in _rg.grip_q("right", 1.0)]}}


def send_home(sink):
    """hold, then home with the hands. The one way home is sent, from anywhere.

    HOLD first: go_home refuses while ARMED ("homing while armed is two
    drivers"), so a home mid-attempt used to do nothing. Holding is the safest
    thing this can send in any state, and it stops hand input reaching the
    arms while they move. Returns False if the sink has no control surface.
    """
    if sink is None or not hasattr(sink, "command"):
        return False
    sink.command("hold")
    try:
        sink.command("home", payload=home_payload())
    except TypeError:
        # A sink whose command() takes no payload (tests, older fakes).
        sink.command("home")
    return True


def grip_thumb0_from_env(env=None):
    """TELEOP_GRIP_THUMB0 -> applied to the live knob at startup; None if unset.

    The knob is how the operator finds which way the thumb opposes on this
    hand. Found, it must survive a redeploy, and a deploy no longer silently
    resets it. Out-of-range or non-finite refuses to start: a wrong thumb on
    the first grip is worse than a bridge that says why it did not come up.
    """
    import retarget_g1 as _rg
    env = os.environ if env is None else env
    raw = str(env.get("TELEOP_GRIP_THUMB0", "")).strip()
    if raw == "":
        return None
    try:
        return _rg.set_grip_thumb0(float(raw))
    except (TypeError, ValueError) as e:
        raise ValueError(f"TELEOP_GRIP_THUMB0={raw!r}: {e}") from None


def headset_buttons(frame, stats, app):
    """The LEFT squeeze starts/stops a session while the headset is the source.

    Face-button attempt and success/fail labelling has been removed. Raw
    demonstrations stay unlabelled until offline replay/annotation, where the
    evidence can be inspected instead of guessed during teleoperation.

    The poses stop at the source gate when anything other than the headset
    owns the sink; this runs in the receive loop, before that gate, so it
    checks the source itself: a squeeze must never start or seal a session
    that some other source is driving. Both stick clicks (arm/home) are
    deliberately NOT gated here: returning home is always a safe way to stop.

    Returns the squeeze's coroutine (start/stop with header, check and seal)
    for the caller to schedule, or None.
    """
    if app.get("source", "xr") != "xr":
        return None
    return record_on_squeeze(frame, stats, app)


def task_from_env(env=None):
    """TELEOP_TASK -> the language instruction every header carries; None if unset.

    Language-conditioned policies train on (video, state, action,
    INSTRUCTION); without the text a session is a movement log. Set once per
    task at deploy; /session/start?task= overrides per session. Unset is None, never
    a guessed sentence.
    """
    env = os.environ if env is None else env
    raw = str(env.get("TELEOP_TASK", "")).strip()
    return raw or None


async def start_session(app, name="", task=None, extra_meta=None):
    """Start a recording. THE one place a session begins.

    `extra_meta` rides in the header beside the run config.

    The header's meta -- arm mode, build, camera, and the whole run
    configuration -- is what makes an episode reproducible. A session started
    anywhere else quietly lacks all of it, and nothing downstream can tell:
    a header without config is also what every episode before that field
    existed looks like.
    """
    rec = app["session"]
    # Refused, not restarted. rec.start() closes any running session with
    # _close_locked(): no check, no seal, no manifest -- an operator recording
    # between attempts lost their episode, unsealed, to a Start on the
    # control page. record_on_squeeze asked `recording` first; other callers
    # did not. Asked HERE, once, so every caller inherits it: stop_session is
    # the only way a recording ends, and it seals.
    if getattr(rec, "recording", False):
        sid = getattr(rec, "_sid", None) or "a session"
        return {"ok": False, "error": f"already recording {sid}; stop it first"}
    task = (str(task).strip() if task is not None else "") or app.get("task") or None
    out = rec.start(name,
                    meta={"arm_mode": app.get("arm_mode"),
                          # The language instruction: what this demonstration
                          # is OF. ?task= per session, else TELEOP_TASK, else
                          # null.
                          "task": task,
                          # Which code recorded this, from the deploy.
                          "build": build_info(),
                          "camera": (app["camera"].status()
                                     if app.get("camera") is not None else None),
                          # Which hand camera was left and which right, by
                          # USB port, for this session.
                          "hand_cameras": await hand_cam_header(app),
                          "config": _run_config(app),
                          **dict(extra_meta or {})})
    print(f"[bridge] session: {out}")
    return out


async def stop_session(app, expected_sid=None):
    """Stop a recording, validate it, seal it. THE one place a session ends.

    The check and the seal are not extras: the training pipeline consumes only
    sealed manifests, and a seal taken here -- before anyone has had a chance
    to touch the file -- is the one that means anything. A session stopped
    anywhere else produces an unsealed episode that looks exactly like a
    legitimate pre-seal one.
    """
    sink = app.get("sink")
    state = str(getattr(sink, "onboard_state", "") or "")
    if app.get("source", "xr") == "xr" and state.startswith("ARMED"):
        return {"ok": False,
                "error": "refused while ARMED; HOLD before stopping and sealing "
                         "a full session"}
    rec = app["session"]
    current_sid = getattr(rec, "_sid", None)
    if expected_sid is not None and current_sid != expected_sid:
        return {"ok": False, "error": "session ownership changed",
                "expected_session": expected_sid, "current_session": current_sid}
    # Off the loop: stop() now waits (bounded, STOP_DRAIN_S) for the writer to
    # land what the session already queued before it writes the footer, so
    # the footer's counts are the file's. That wait must not stall the frame
    # path, which shares this loop.
    out = (await asyncio.to_thread(rec.stop, expected_sid)
           if expected_sid is not None else await asyncio.to_thread(rec.stop))
    print(f"[bridge] session: {out}")
    # Validate the file just written, so a bad episode is flagged the moment
    # it exists rather than the day someone trains on it. Off the event loop:
    # a 20 MB session takes a second or two to read.
    path = rec.path(out.get("session") or "") if out.get("ok") and hasattr(rec, "path") else None
    if path:
        import episode_check
        try:
            out["check"] = await asyncio.to_thread(episode_check.check, path)
            print("[bridge] session check: " + episode_check.summary(out["check"]))
        except Exception as e:  # noqa: BLE001 -- the check must not break the stop
            out["check"] = {"ok": False, "error": str(e)}
        import episode_seal
        try:
            m = await asyncio.to_thread(episode_seal.seal, path)
            out["manifest"] = {"path": episode_seal.manifest_path(path),
                               "schema": m["schema"],
                               "sha256": m["episode"]["sha256"]}
            print("[bridge] session sealed: " + episode_seal.summary(m))
        except Exception as e:  # noqa: BLE001 -- nor must the seal
            out["manifest"] = {"ok": False, "error": str(e)}
    return out


async def session_handler(request: web.Request) -> web.Response:
    """Start/stop/list/fetch a full session recording.

    Distinct from /record/*, which keeps raw landmarks in memory for a quick
    offline sweep. This one writes JSONL to disk, carries the retargeter's
    OUTPUT alongside the input, and survives a restart -- so a session can be
    diffed against a code change rather than only replayed.
    """
    rec = request.app.get("session")
    if rec is None:
        return web.json_response({"ok": False, "error": "no recorder"}, status=409)
    step = request.match_info["step"]
    if step in ("mark", "outcome"):
        return web.json_response({"ok": False,
                                  "error": "manual attempt and success/fail labels were removed; annotate offline"},
                                 status=410)
    # start/stop change state; get serves a recording off the persist volume.
    # status and list stay open for the control page.
    if step in ("start", "stop", "get", "frames", "manifest"):
        if (refused := _locked(request)) is not None:
            return refused
    if step == "start":
        return web.json_response(await start_session(request.app,
                                                     request.query.get("name", ""),
                                                     task=request.query.get("task")))
    if step == "stop":
        return web.json_response(await stop_session(request.app))
    if step == "status":
        return web.json_response(rec.status())
    if step == "list":
        return web.json_response({"sessions": rec.sessions()})
    if step == "get":
        path = rec.path(request.query.get("session", ""))
        if path is None:
            return web.json_response({"ok": False, "error": "no such session"},
                                     status=404)
        return web.FileResponse(path)
    if step == "frames":
        # The camera frames, as one tar.
        #
        # They live in <sid>.frames/ inside the bridge's container, and the
        # container is replaced on every deploy. /session/get returned the
        # .jsonl only, so an episode could be pulled, committed and SEALED
        # while the 2361 images its rows point at stayed behind to be deleted
        # by the next redeploy -- which is the whole reason the camera exists.
        # The seal notices (`camera frames 0`), but only after the fact.
        sid = request.query.get("session", "")
        path = rec.path(sid)
        if path is None:
            return web.json_response({"ok": False, "error": "no such session"},
                                     status=404)
        base = path[:-3] if path.endswith(".gz") else path
        frames_dir = base[:-len(".jsonl")] + ".frames" if base.endswith(".jsonl") else None
        if not frames_dir or not os.path.isdir(frames_dir):
            return web.json_response({"ok": False, "session": sid,
                                      "error": "no camera frames for this session"},
                                     status=404)
        # Built in a worker thread: with the hand cameras a session's frames
        # (hand-left/, hand-right/ inside this directory) run to hundreds of
        # MB, and tarring them on the event loop froze the 90 Hz pose path
        # for as long as it took.
        def build_tar():
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tf:
                tf.add(frames_dir, arcname=os.path.basename(frames_dir))
            return buf.getvalue()
        blob = await asyncio.to_thread(build_tar)
        return web.Response(body=blob, content_type="application/x-tar",
                            headers={"Content-Disposition":
                                     f'attachment; filename="{os.path.basename(frames_dir)}.tar"'})
    if step == "manifest":
        # The sealed manifest, so a consumer pulls the seal together with
        # the file and can verify the pair on its own machine.
        path = rec.manifest_path(request.query.get("session", "")) if hasattr(rec, "manifest_path") else None
        if path is None:
            return web.json_response({"ok": False, "error": "no manifest for that session"},
                                     status=404)
        return web.FileResponse(path)
    return web.json_response({"ok": False, "error": f"unknown step {step}"},
                             status=400)


async def record_handler(request: web.Request) -> web.Response:
    """Capture RAW hand landmarks so retargeting can be tested offline.

    Every live A/B of a candidate rotation has been an uncontrolled experiment:
    the same matrix scored 57 % and 4.7 % on consecutive runs because the
    operator moved differently. One recording, replayed against every
    candidate, removes the operator from the comparison entirely -- and becomes
    a permanent regression fixture that needs neither a headset nor a robot.
    """
    if (refused := _locked(request)) is not None:
        return refused
    st = request.app["stats"]
    step = request.match_info["step"]
    if step == "start":
        st["_rec"] = []
        st["_rec_on"] = True
        return web.json_response({"ok": True, "msg": "recording landmarks"})
    if step == "stop":
        st["_rec_on"] = False
        return web.json_response({"ok": True, "frames": len(st.get("_rec") or [])})
    if step == "dump":
        return web.json_response({"frames": st.get("_rec") or []})
    return web.json_response({"ok": False, "error": f"unknown step {step}"},
                             status=400)


async def fingercal_handler(request: web.Request) -> web.Response:
    """Finger calibration lives in the BRIDGE, not the robot.

    It is a property of the operator's hand and the retargeting, not of the
    robot, and putting it here means it can be redone without a robot deploy --
    which today would be impossible anyway, with no build host reachable.
    """
    r = request.app["retargeter"]
    step = request.match_info["step"]
    if step == "dump":
        cal = getattr(r, "_cal", None) or {}
        return web.json_response({"joints": {k: list(v) for k, v in cal.items()}})
    # Everything below rewrites the live finger mapping.
    if (refused := _locked(request)) is not None:
        return refused
    fn = {"start": lambda: r.cal_start(),
          "open": lambda: r.cal_mark("open"),
          "closed": lambda: r.cal_mark("closed"),
          "finish": lambda: r.cal_finish(),
          "clear": lambda: r.cal_clear()}.get(step)
    if fn is None:
        return web.json_response({"ok": False, "error": f"unknown step {step}"},
                                 status=400)
    if not hasattr(r, "cal_start"):
        return web.json_response({"ok": False,
                                  "error": "this retargeter has no finger calibration"},
                                 status=409)
    msg = fn()
    print(f"[bridge] fingercal {step}: {msg}")
    return web.json_response({"ok": not msg.startswith("refused"), "msg": msg})


_ONBOARD_ORDER = list(ARM_LEFT + ARM_RIGHT)   # the onboard's 14-vector order


async def camera_frame_handler(request: web.Request) -> web.Response:
    """The newest head-camera JPEG, for the headset's overlay.

    The operator drives inside a headset looking at the real robot through
    passthrough, and has no way to see what the ROBOT sees -- which is the
    view a recorded demonstration is trained on. This is that image, small
    and cacheless, polled by the page a few times a second.
    """
    cam = request.app.get("camera")
    latest = getattr(cam, "_latest", None) if cam is not None else None
    if not latest:
        return web.Response(status=404, text="no camera frame")
    header, jpeg, _t = latest
    return web.Response(body=jpeg, content_type="image/jpeg",
                        headers={"Cache-Control": "no-store",
                                 "X-Frame-Seq": str(header.get("seq", "")),
                                 "X-Frame-Id": str(header.get("frame_id", ""))})


# -- Hand cameras (cameras/hand_cameras.py, port 8091) --------------------- #
# Proxied, not linked: the console is served over HTTPS and the camera service
# speaks plain HTTP on the robot, and a browser blocks that mix. Every handler
# answers even when the service is absent, so a robot without hand cameras
# shows an empty panel instead of errors.
HAND_CAM_NAMES = ("left", "right")


def _hand_cam_base() -> str:
    return os.environ.get("TELEOP_HAND_CAM_URL", "http://127.0.0.1:8091").rstrip("/")


def _hand_cam_http(app):
    import aiohttp
    s = app.get("hand_cam_http")
    if s is None or s.closed:
        s = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.0))
        app["hand_cam_http"] = s
    return s


async def hand_camera_status_handler(request: web.Request) -> web.Response:
    """The hand-camera service's /status, or {"enabled": false} without it."""
    try:
        async with _hand_cam_http(request.app).get(_hand_cam_base() + "/status") as r:
            data = await r.json()
    except Exception as e:  # noqa: BLE001 -- no service is a state, not an error
        data = {"enabled": False,
                "error": f"hand-camera service unreachable ({type(e).__name__})"}
    return web.json_response(data, headers={"Cache-Control": "no-store"})


async def hand_camera_frame_handler(request: web.Request) -> web.Response:
    """The newest JPEG from one hand camera, for the console."""
    name = request.match_info["name"]
    if name not in HAND_CAM_NAMES:
        return web.Response(status=404, text="no such camera")
    try:
        url = f"{_hand_cam_base()}/cam/{name}/frame.jpg"
        async with _hand_cam_http(request.app).get(url) as r:
            if r.status != 200:
                return web.Response(status=404, text="no frame")
            body = await r.read()
            headers = {"Cache-Control": "no-store"}
            for h in ("X-Frame-Seq", "X-Captured-Mono-Ns", "X-Captured-Unix-Ns"):
                if h in r.headers:
                    headers[h] = r.headers[h]
    except Exception:  # noqa: BLE001
        return web.Response(status=503, text="hand-camera service unreachable")
    return web.Response(body=body, content_type="image/jpeg", headers=headers)


def hand_cams_enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get("TELEOP_HAND_CAMS", "off")).strip().lower() not in ("0", "off", "no", "false", "")


def hand_cam_refs(app):
    """{name: {seq, t_cap_mono_ns, age_s}} of each hand camera's newest frame,
    for the pose row, or None when there are none."""
    clients = app.get("hand_cams") or {}
    refs = {name: c.latest_ref() for name, c in clients.items()}
    refs = {name: r for name, r in refs.items() if r is not None}
    return refs or None


async def hand_cam_header(app):
    """Which camera was which for this session: names, USB ports, devices.

    Written into the session header. Left and right are a choice an operator
    made with the Swap button, and a recording must say which choice it was
    made under; a cable moved later cannot rewrite it."""
    if not app.get("hand_cams"):
        return None
    try:
        async with _hand_cam_http(app).get(_hand_cam_base() + "/status") as r:
            st = await r.json()
    except Exception as e:  # noqa: BLE001 -- a header without it says so
        return {"error": f"hand-camera service unreachable ({type(e).__name__})"}
    return {name: {k: cam.get(k) for k in ("port", "path", "device", "camera", "w", "h", "fps")}
            for name, cam in (st.get("cameras") or {}).items()}


async def hand_camera_swap_handler(request: web.Request) -> web.Response:
    """Swap which camera is called left and which right (saved on the robot)."""
    locked = _locked(request)
    if locked is not None:
        return locked
    try:
        async with _hand_cam_http(request.app).post(_hand_cam_base() + "/swap") as r:
            data, status = await r.json(), r.status
    except Exception as e:  # noqa: BLE001
        return web.json_response(
            {"ok": False, "msg": f"hand-camera service unreachable ({type(e).__name__})"},
            status=503)
    return web.json_response(data, status=status)


async def control_handler(request: web.Request) -> web.Response:
    """Drive the onboard state machine from a browser.

    The only way to arm used to be typing at the sink's stdin -- which does not
    exist in a detached container, so a deployed bridge could never arm the
    robot at all. This is that surface, reachable from a phone.
    """
    sink = request.app["sink"]
    name = request.match_info["cmd"]
    if not _control_allowed(request):
        print(f"[bridge] REFUSED {name}: bad or missing control key")
        return web.json_response({"ok": False, "error": "bad or missing key"},
                                 status=403)
    if not hasattr(sink, "command"):
        return web.json_response({"ok": False, "error": f"sink has no control surface"},
                                 status=409)
    if name == "home":
        # With the closed hand pose the grip would command, so home closes the
        # hands the way a squeeze does -- one owner of "closed".
        ok = sink.command(name, payload=home_payload())
    else:
        if name == "arm":
            r = request.app.get("retargeter")
            if hasattr(r, "prepare_controller_grips"):
                r.prepare_controller_grips(getattr(sink, "last_hand_q", None))
        if name in ("take_control", "arm"):
            _note_control_event(request.app, "control.acquire.requested",
                                command=name, source="control_api")
        if name in ("stop", "relax"):
            _note_control_event(request.app, "control.release.requested",
                                command=name, source="control_api")
        ok = sink.command(name)
    if not ok:
        return web.json_response({"ok": False, "error": f"unknown command {name}"},
                                 status=400)
    return web.json_response({"ok": True, "queued": name})


def _live_fps(st) -> float:
    """The headset's frame rate, decayed when frames stop.

    `fps` is only recomputed when a frame ARRIVES, so a stream that stops
    leaves the last value standing forever -- a dead link reads as a healthy
    90 fps. That is not a cosmetic problem: it cost a wrong diagnosis tonight
    ("the page is streaming but the poses are stale") when in truth nothing
    was arriving at all and every counter was frozen.

    Recomputing over the open window makes a stall decay toward zero on its
    own, and leaves a healthy stream reading exactly as before.
    """
    since = st.get("since")
    fps = st.get("fps", 0.0) or 0.0
    if not since:
        return fps
    dt = time.monotonic() - since
    if dt < 2.0:                     # inside the normal 1 s window: trust it
        return fps
    return round((st.get("frames", 0) or 0) / dt, 1)


def xr_stream_status(st, now=None) -> dict:
    """Classify the Quest side without mistaking an open socket for poses.

    The browser heartbeat runs independently of hand/controller tracking.  A
    fresh heartbeat with no pose means tracking is gone; no heartbeat means
    the page/XR session itself is asleep or stalled.  This distinction cannot
    be reconstructed from TCP connected state alone.
    """
    now = time.monotonic() if now is None else float(now)

    def age(key):
        t = st.get(key)
        return round(max(0.0, now - float(t)), 3) if t else None

    heartbeat_age = age("last_heartbeat_t")
    pose_age = age("last_pose_t")
    client = dict(st.get("client") or {})
    if not st.get("connected") or not st.get("clients"):
        state = "disconnected"
    elif heartbeat_age is None or heartbeat_age > 3.0:
        state = "client_stalled"
    elif pose_age is None or pose_age > 0.5:
        state = "tracking_lost" if client.get("tracked_inputs") == 0 else "pose_stalled"
    else:
        state = "healthy"
    return {"state": state, "heartbeat_age_s": heartbeat_age,
            "pose_age_s": pose_age, "client": client}


async def control_status_handler(request: web.Request) -> web.Response:
    sink = request.app["sink"]
    st = request.app["stats"]
    out = {"xr_connected": st.get("connected", False),
           # More than one is worth seeing: a stale tab that keeps
           # reconnecting is indistinguishable from a flapping link unless
           # the count is on the page.
           "xr_clients": st.get("clients", 0),
           "xr_fps": _live_fps(st),
           "xr_hands": st.get("hands", 0),
           "xr_sources": st.get("sources"),
           "xr_stream": xr_stream_status(st),
           # The headset's legend says "start recording" or "STOP recording"
           # depending on this. Without it the operator, who cannot see the
           # laptop, is told to start a session that is already running.
           "session": ({"recording": bool(getattr(request.app["session"], "recording", False)),
                        "id": getattr(request.app["session"], "_sid", None)}
                       if request.app.get("session") is not None else None),
           # TELEOP_RECORDING. The control page and the headset legend hide
           # their recording controls when this is False.
           "recording_enabled": request.app.get("session") is not None,
           "arm_targets": st.get("arm_targets", 0),
           "arm_moving": st.get("arm_moving"),
           "arm_travel_max": (max(st["arm_travel"]) if st.get("arm_travel") else None),
           "arm_now": st.get("arm_now"),
           "fingers": st.get("fingers"),
           "fabricated": st.get("fabricated"),
           "fabricated_avg": st.get("fabricated_avg"),
           "fabricated_max": st.get("fabricated_max"),
           # Async IK, when it is on: solved/dropped says whether the worker is
           # keeping up, and age_s is the honest latency of the arm targets.
           "arm_ik": (request.app["retargeter"]._ik_worker.stats()
                      if getattr(request.app["retargeter"], "_ik_worker", None)
                      else None),
           "wrist_gaps": getattr(request.app["retargeter"], "n_wrist_gaps", None),
           "arm_mode": request.app.get("arm_mode"),
           "smoothing": ({"mode": getattr(request.app["retargeter"], "smooth_mode", None),
                           "min_cutoff": getattr(request.app["retargeter"],
                                                 "smooth_min_cutoff", None),
                           "beta": getattr(request.app["retargeter"], "smooth_beta", None),
                           "tau": getattr(request.app["retargeter"], "smooth_tau", None),
                           "joint_cost": dict(getattr(request.app["retargeter"],
                                                      "smooth_joint_cost", {}) or {})}
                         if hasattr(request.app["retargeter"], "set_smooth") else None),
           "build": build_info(),
           "waist": (request.app["waist"].status()
                     if request.app.get("waist") is not None else None),
           "waist_frozen": bool(request.app.get("waist_frozen")),
           "locomotion": getattr(sink, "onboard_locomotion", None),
           "locomotion_permission": walking_permission(request.app).status(),
           "controller_wrist_gain": (request.app["controller_wrist_gain"].status()
                                     if request.app.get("controller_wrist_gain") is not None else None),
           "camera": (request.app["camera"].status()
                      if request.app.get("camera") is not None else None),
           "trigger_grip": (request.app["retargeter"]._trigger_grip.status()
                            if getattr(request.app["retargeter"], "_trigger_grip", None)
                            else None),
           # Shoulder solutions that had to be clamped to the robot's limits
           # (per side: how far outside, in rad, the last one sat). The number
           # the first full-stack episode was missing: 58 % of the right
           # shoulder commands were unreachable and nothing here said so.
           "shoulder_unreachable": getattr(request.app["retargeter"], "n_shoulder_clamped", None),
           "shoulder_miss": getattr(request.app["retargeter"], "_shoulder_miss", None),
           "source": request.app.get("source", "xr"),
           "xr_frames_ignored": st.get("xr_frames_ignored", 0),
           "finger_cal": ("recording" if getattr(request.app["retargeter"], "_cal_rec", None)
                          else f"on ({len(getattr(request.app['retargeter'], '_cal', None) or {})} joints)"
                          if getattr(request.app["retargeter"], "_cal", None) else "off")}
    if not hasattr(sink, "status"):
        out.update(sink=type(sink).__name__, state="n/a")
    else:
        out.update(sink.status())
    # The spectator reads the same visual packet as Quest without opening a
    # teleop websocket or taking control ownership.
    out["robot_view"] = robot_state_message(sink)
    return web.json_response(out)


def make_retargeter_fingers_only(r):
    """Flip the retargeter into fingers-only. Loud, because it silently
    disables all arm motion."""
    r.fingers_only = True
    print("[bridge] FINGERS ONLY — the arm IK is skipped and no arm joints are "
          "put on the wire. The robot cannot be commanded to move its arms.")
    return r


def make_sink(kind: str, world: str, realg1_url: str = "") -> SimSink:
    if kind == "realg1":
        from sink_realg1 import RealG1Sink   # real hardware — streams to onboard commander
        return RealG1Sink(realg1_url)
    if kind == "sim":
        return DirectSimSink(world)
    return LogSink()


# --------------------------------------------------------------------------- #
# WebSocket loop — latest-wins so we never process stale frames.
# --------------------------------------------------------------------------- #
async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)
    sink: SimSink = request.app["sink"]
    retargeter = request.app["retargeter"]
    stats = request.app["stats"]
    # Count the clients rather than holding one boolean.
    #
    # A stale tab in the headset's browser keeps retrying its socket -- the
    # page reconnects every second while its XR session lives -- so with two
    # pages open, one connecting set `connected` True and the other dropping
    # set it False, over and over. The status read as a headset flapping on
    # and off twice a second while the live page sat there sending nothing,
    # and it took an evening to stop reading that as a network fault.
    #
    # With a count, a second client cannot report the first as gone.
    stats["clients"] = stats.get("clients", 0) + 1
    connected_t = time.monotonic()
    stats.update(connected=True, frames=0, hands=0, fps=0.0, since=connected_t,
                 connected_t=connected_t, last_heartbeat_t=0.0, last_pose_t=0.0,
                 client={})
    _note_control_event(request.app, "headset.connected", clients=stats["clients"])
    print(f"[bridge] Quest connected ({stats['clients']} client(s))")
    if stats["clients"] > 1:
        print(f"[bridge] NOTE: {stats['clients']} pages are connected. A stale tab in "
              "the headset's browser will keep reconnecting; close the others.")

    def _log_hello(frame):
        """The client reports what its XR session actually became.

        environmentBlendMode is the ground truth for passthrough: "opaque" means
        a black void no matter what mode was asked for; "additive" or
        "alpha-blend" means the real room is showing through."""
        if not isinstance(frame, dict) or frame.get("type") != "hello":
            return False
        print(f"[bridge] headset: requested={frame.get('requested')} "
              f"blend={frame.get('environmentBlendMode')} "
              f"interaction={frame.get('interactionMode')} "
              f"input={frame.get('input', 'auto')} "
              f"support={frame.get('support')}")
        print(f"[bridge] ua={frame.get('ua')}")
        return True

    def _note_heartbeat(frame):
        if not isinstance(frame, dict) or frame.get("type") != "heartbeat":
            return False
        stats["last_heartbeat_t"] = time.monotonic()
        diagnostics = request.app.get("control_events")
        if diagnostics:
            fields = {k: frame.get(k) for k in ("visibility", "xr_visibility", "tracked_inputs")}
            diagnostics.transition("xr_visibility", tuple(fields.values()), "headset.tracking", **fields)
        # Whitelist and bound what /control/status echoes.  This is diagnostic
        # state from a browser, not an arbitrary object to retain forever.
        stats["client"] = {
            k: frame.get(k) for k in (
                "raf_fps", "pose_fps", "camera_fps", "tracked_inputs", "buffered_bytes",
                "dropped_backpressure", "render_errors", "send_errors",
                "visibility", "xr_visibility", "hud_mode", "hud_resets")
        }
        return True
    latest = None
    # None when TELEOP_RECORDING is off: no row is built on the pose path.
    session = request.app.get("session")

    async def drain():
        nonlocal latest
        while not ws.closed:
            if latest is not None:
                frame, latest = latest, None
                try:
                    # X changes only controller-space fore/aft translation.
                    # Keep `frame` raw for the recorder; only this derived copy
                    # reaches IK. Outside live XR/ARMED the module resets.
                    armed = str(getattr(sink, "onboard_state", "")).startswith("ARMED")
                    controller_wrist_gain = request.app["controller_wrist_gain"]
                    control_frame, wrist_gain_observation = controller_wrist_gain.transform(
                        frame,
                        armed=armed and request.app.get("source", "xr") == "xr")
                    targets = retargeter.process(control_frame)
                    # The waist rides in the same dict as the arm joints, so
                    # the row, `sent` and the wire all see one command.
                    waist_on_thumbstick(frame, stats, request.app, targets)
                    locomotion_on_thumbstick(frame, request.app)
                    _note_targets(stats, targets)
                    # Only the headset may drive the sink. Were another source
                    # selected, headset frames would still be processed for the
                    # status page, but nothing from them would reach the robot
                    # or be recorded as what the robot was told.
                    if request.app.get("source", "xr") != "xr":
                        stats["xr_frames_ignored"] = stats.get("xr_frames_ignored", 0) + 1
                        continue
                    # Recorded HERE, with the frame that produced them, rather
                    # than at ingest: input alone lets a session be replayed,
                    # input plus output lets it be diffed against a code
                    # change, which is the question during a regression.
                    # The row is written AFTER apply(): the sink subtracts the
                    # arming offset inside apply, and the row must carry what
                    # was actually queued for this frame (`sent`), not only
                    # what the operator meant (`targets`). apply() does not
                    # await anything and does not touch the frame, so nothing
                    # the row records has changed by the time it is read.
                    before = sent_before(sink)
                    await sink.apply(targets)
                    view = getattr(sink, "robot_view", None)
                    robot_view = view() if view else {
                        "state": getattr(sink, "onboard_state", None),
                        "msg": getattr(sink, "onboard_msg", None) or None,
                        "q": getattr(sink, "onboard_q", None),
                        "hand_q": getattr(sink, "last_hand_q", None),
                    }
                    sent_row = sent_for_row(sink, before)
                    if session is not None:
                        # The onboard's own view, per frame. Without it a
                        # frozen stretch is indistinguishable from a
                        # demonstration of holding still.
                        session.note(frame, targets, robot_view,
                        wrist_ref=(retargeter.wrist_reference()
                                      if hasattr(retargeter, "wrist_reference")
                                      else None),
                        # Which camera frame was current for this row.
                        cam=(request.app["camera"].latest_ref()
                             if request.app.get("camera") is not None else None),
                        # And which frame from each hand camera.
                        hand_cams=hand_cam_refs(request.app),
                        # What the robot was TOLD for this frame, and the
                        # constant between that and `targets`.
                        sent=sent_row,
                        arm_offset=arm_offset_for_row(sink),
                        wrist_gain=wrist_gain_observation)
                except Exception as e:
                    print("[bridge] process/sink error:", e)
            else:
                await asyncio.sleep(0.001)

    drain_task = asyncio.create_task(drain())
    # The return path: where the robot is, 30 times a second, for the
    # miniature the page draws. See robot_state_message.
    push_task = asyncio.create_task(push_robot_state(ws, sink, app=request.app))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    parsed = json.loads(msg.data)
                    if isinstance(parsed, dict):
                        parsed["bridge_received_unix_ns"] = time.time_ns()
                        parsed["bridge_received_monotonic_ns"] = time.monotonic_ns()
                    # The capability report is not a pose frame; log it and stop.
                    if _log_hello(parsed) or _note_heartbeat(parsed):
                        latest = None
                    else:
                        latest = parsed
                    if latest is not None:
                        stats["last_pose_t"] = time.monotonic()
                        # How many hands the headset SAW, per frame. Without
                        # this the only symptom of lost tracking was silence,
                        # which looks identical to a broken WebSocket.
                        stats["hands"] = len(latest.get("hands") or ())
                        # The LEFT squeeze toggles the recording and obeys the
                        # source gate the poses obey. Scheduled, not
                        # awaited: a seal takes seconds and the frame loop
                        # cannot wait for it.
                        _arm_task = arm_on_sticks(latest, stats, request.app)
                        if _arm_task is not None:
                            asyncio.ensure_future(_arm_task)
                        _rec_task = headset_buttons(latest, stats, request.app)
                        if _rec_task is not None:
                            asyncio.ensure_future(_rec_task)
                        # ... and the right stick HELD hands the robot back.
                        # Ungated on the source for the same reason as home:
                        # it stops this bridge driving, which any source in
                        # progress should honour rather than outlive.
                        release_on_stick_hold(latest, stats, request.app)
                        # Tracked hand or controller, per side. A mixed frame
                        # is a real state, not one hand missing.
                        stats["sources"] = {h.get("hand"): (h.get("source") or "hand")
                                            for h in (latest.get("hands") or ())}
                        # Landmarks the headset could not see. Wrist-local,
                        # (0,0,0) is the fingertip AT the wrist -- full curl --
                        # so these become confident grasps downstream.
                        fab = {h.get("hand"): h.get("fabricated", 0)
                               for h in (latest.get("hands") or ())}
                        stats["fabricated"] = fab
                        # Kept so /elbowcal can measure reach at the moment the
                        # operator says "this is my near end", rather than
                        # whatever frame happens to arrive next.
                        stats["_last_wrists"] = {
                            h["hand"]: h["wrist"]
                            for h in (latest.get("hands") or ())
                            if h.get("hand") and h.get("wrist")}
                        hist = stats.setdefault("_fab_hist", [])
                        hist.append(sum(v or 0 for v in fab.values()))
                        if len(hist) > 300:
                            del hist[:-300]
                        stats["fabricated_avg"] = round(sum(hist) / len(hist), 2)
                        stats["fabricated_max"] = max(hist)
                        if stats.get("_rec_on"):
                            rec = stats.setdefault("_rec", [])
                            if len(rec) < 4000:      # ~45 s at 90 fps
                                rec.append([
                                    {"hand": h.get("hand"),
                                     "landmarks": h.get("landmarks"),
                                     # wrist pose too: the forearm mapping is
                                     # derived from orientation, and a
                                     # recording without it cannot settle it.
                                     "wrist": h.get("wrist"),
                                     "quat": h.get("quat")}
                                    for h in (latest.get("hands") or ())])
                        stats["frames"] += 1
                        dt = time.monotonic() - stats["since"]
                        if dt >= 1.0:
                            stats["fps"] = round(stats["frames"] / dt, 1)
                            print(f"[bridge] {stats['fps']} fps, "
                                  f"{stats['hands']} hand(s)")
                            stats["frames"], stats["since"] = 0, time.monotonic()
                except json.JSONDecodeError:
                    pass
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        _note_control_event(request.app, "headset.disconnected", clients=max(0, stats.get("clients", 1)-1))
        drain_task.cancel()
        push_task.cancel()
        stats["clients"] = max(0, stats.get("clients", 1) - 1)
        if stats["clients"] == 0:
            stats.update(connected=False, fps=0.0, hands=0)
        print(f"[bridge] Quest disconnected ({stats['clients']} still connected)")
    return ws


def recording_enabled(env=None) -> bool:
    """TELEOP_RECORDING: data collection on (default) or off.

    Off (0/off/false/no): no SessionRecorder is built, so nothing is written
    for a session; /session/*, /record/* are not registered (404); and
    /control/status reports `recording_enabled: false`, which the control
    page and the headset legend read to hide their recording controls.
    Teleoperation itself is identical either way.
    """
    env = os.environ if env is None else env
    raw = str(env.get("TELEOP_RECORDING", "1")).strip().lower()
    return raw not in ("0", "off", "false", "no")


def make_app(retargeter, sink: SimSink, recording=None) -> web.Application:
    from lan_access import lan_access
    app = web.Application(middlewares=[lan_access])
    app["lan_https_required"] = False
    app["retargeter"] = retargeter
    app["sink"] = sink
    from control_events import ControlEvents
    app["control_events"] = ControlEvents()
    sink.control_events = app["control_events"]
    async def close_control_events(app):
        await asyncio.to_thread(app["control_events"].close)
    app.on_cleanup.append(close_control_events)
    async def close_hand_cam_http(app):
        s = app.get("hand_cam_http")
        if s is not None and not s.closed:
            await s.close()
    app.on_cleanup.append(close_hand_cam_http)
    app["stats"] = {"connected": False, "frames": 0, "hands": 0,
                    "fps": 0.0, "since": 0.0}
    recording = recording_enabled() if recording is None else bool(recording)
    app["recording_enabled"] = recording
    # TELEOP_SESSION_DIR points at a persist volume on the device, so
    # recordings outlive a redeploy. Falls back to a local path for a laptop
    # run, where losing them on restart costs nothing.
    app["session"] = (SessionRecorder(
        SessionRecorder.pick_dir(os.environ.get("TELEOP_SESSION_DIR", "")))
        if recording else None)
    print("[bridge] recording: "
          + (f"on -> {app['session'].dir} (TELEOP_RECORDING=0 to disable)" if recording
             else "OFF (TELEOP_RECORDING=0): no sessions are written"))
    app["control_key"] = ""
    # Which input feeds the sink. Always the headset in this build; the
    # gates that check it stay so nothing but live XR can drive the robot.
    app["source"] = "xr"
    walking_permission(app)
    from controller_wrist_gain import ControllerWristGain
    app["controller_wrist_gain"] = ControllerWristGain.from_env()
    wrist_gain = app["controller_wrist_gain"].status()
    if not app["controller_wrist_gain"].enabled:
        print("[bridge] held X does nothing (the fore/aft gain is disabled)")
    else:
        print(f"[bridge] hold X for controller wrist fore/aft gain: "
              f"back {wrist_gain['backward_gain']}x, forward "
              f"{wrist_gain['forward_gain']}x, deadband "
              f"{wrist_gain['deadband_m'] * 1000:.1f} mm, max "
              f"{wrist_gain['max_velocity_m_s']:.2f} m/s")
    # Waist yaw on the right thumbstick. TELEOP_WAIST_RATE_RAD_S (0.5) and
    # TELEOP_WAIST_MAX_DEG (143, the joint's whole travel). A bad value
    # refuses to start rather than turning the waist at a rate nobody chose.
    # The onboard's G1_WAIST_MAX_RAD is the safety bound and always wins, so
    # asking for more here just gets clamped there.
    from waist import WaistDial
    app["waist"] = WaistDial.from_env()
    # The operator's waist lock (control page, /waist). TELEOP_WAIST_FROZEN=1
    # starts the bridge locked, for a demo that never wants the torso turned.
    app["waist_frozen"] = str(os.environ.get("TELEOP_WAIST_FROZEN", "")).strip().lower() in ("1", "true")
    print(f"[bridge] waist yaw on the right stick: "
          f"+-{app['waist'].status()['cap_deg']:.0f} deg at "
          f"{app['waist'].rate} rad/s")
    app.router.add_get("/teleop", ws_handler)
    app.router.add_get("/control/status", control_status_handler)
    app.router.add_post("/wristcal/flip/{side}/{axis}", wristcal_handler)
    app.router.add_post("/wristcal/zero", wristcal_zero_handler)
    app.router.add_post("/elbowcal/{which}", elbowcal_handler)
    if recording:
        app.router.add_post("/session/{step}", session_handler)
        app.router.add_get("/session/{step}", session_handler)
        app.router.add_post("/record/{step}", record_handler)
        app.router.add_get("/record/{step}", record_handler)
    app.router.add_get("/camera/frame.jpg", camera_frame_handler)
    app.router.add_get("/hand-camera/status", hand_camera_status_handler)
    app.router.add_get("/hand-camera/{name}.jpg", hand_camera_frame_handler)
    app.router.add_post("/hand-camera/swap", hand_camera_swap_handler)
    app.router.add_post("/fingercal/{step}", fingercal_handler)
    app.router.add_get("/fingercal/{step}", fingercal_handler)
    app.router.add_get("/waist", waist_lock_handler)
    app.router.add_post("/waist", waist_lock_handler)
    app.router.add_get("/locomotion", locomotion_permission_handler)
    app.router.add_post("/locomotion", locomotion_permission_handler)
    app.router.add_post("/control/{cmd}", control_handler)
    app.router.add_get("/", lambda r: web.HTTPFound("/index.html"))
    app.router.add_static("/", WEB_DIR, show_index=False)
    app.on_response_prepare.append(_no_stale_web_code)
    return app


_WEB_CODE_SUFFIXES = (".html", ".js", ".css", ".json", ".svg")


async def _no_stale_web_code(request, response):
    """Make the headset revalidate the page's code on every load.

    Served without a Cache-Control header, Quest Browser may reuse an old
    teleop.js after a redeploy: the operator reloaded and kept running the
    previous HUD. `no-cache` still lets it keep a copy -- a reload is a quick
    304 against the ETag -- but never without asking the bridge first.
    """
    if (request.method == "GET" and "Cache-Control" not in response.headers
            and request.path.endswith(_WEB_CODE_SUFFIXES)):
        response.headers["Cache-Control"] = "no-cache"


def _measured_waist(sink):
    """Motor 12's measured angle out of the onboard's body vector, or None."""
    body = getattr(sink, "onboard_body", None)
    if not isinstance(body, dict):
        return None
    idx, q = body.get("idx") or [], body.get("q") or []
    try:
        return float(q[list(idx).index(12)])
    except (ValueError, IndexError, TypeError):
        return None


def robot_state_message(sink, app=None):
    """One "robot" message for the headset: where the robot IS and what it was TOLD.

    The VR page draws a miniature from it -- measured pose solid, the command
    as a ghost -- so the operator can see the hands when they are outside the
    head camera's cone, and see lag and the leash as the gap between the two.

    The command is sink.commanded(), NOT the retargeter's targets: apply()
    subtracts the arming offset (53.8 deg on an elbow, measured) before
    anything reaches the onboard, so the raw targets drawn against the
    measured arm would show that offset as a permanent gap on a joint nothing
    is touching.

    A partial command (forearm-only modes) keeps the 14 slots with None in
    the joints it does not drive; the page indexes by arm_order. Nothing
    here is hidden for being stale -- q_age_s is stamped and the page refuses
    to draw an old measurement as the robot's pose.

    Also what the headset turns into feedback: the joints the onboard clamped
    at a limit or the leash held (`clamped`, `leashed`), why it refused the
    frame (`refused`), the finger joints backed off as stalled
    (`hand_backoff`) and, with `app`, the head camera's newest frame and its
    age (`cam`). Every one of these used to be silent inside the headset: the
    operator learned of a leash from the arm lagging, and of a stalled finger
    from nothing at all.

    getattr throughout: a LogSink or SimSink has none of this, and the push
    must say so rather than break the socket that carries the hand frames.
    """
    q_t = getattr(sink, "onboard_q_t", 0.0) or 0.0
    cmd = sink.commanded() if hasattr(sink, "commanded") else None
    arm = None
    if cmd:
        arm = [cmd.get(n) for n in _ONBOARD_ORDER]
        if all(v is None for v in arm):
            arm = None
    hands = sink.last_hands() if hasattr(sink, "last_hands") else None
    if hands is not None:
        hands = {"left": list(hands[0]), "right": list(hands[1])}
    state = getattr(sink, "onboard_state", None)
    reference = getattr(sink, "onboard_arm_reference", None)
    if reference:
        reference = dict(reference)
        reference["age_s"] = float(reference.get("age_s", 0)) + max(
            0., time.monotonic() - getattr(sink, "onboard_arm_reference_t", 0.))
    msg = {
        "type": "robot",
        "state": state,
        "arm_reference": reference,
        "q": getattr(sink, "onboard_q", None),
        "q_age_s": round(time.monotonic() - q_t, 3) if q_t else None,
        "waist": _measured_waist(sink),
        "hand_q": getattr(sink, "last_hand_q", None),
        "cmd": {"arm": arm,
                "waist": (cmd or {}).get("waist_yaw_joint"),
                "hands": hands},
        "clamped": list(getattr(sink, "last_clamped", None) or []),
        "leashed": list(getattr(sink, "last_leashed", None) or []),
        "refused": getattr(sink, "last_refused", None),
        # The most recent raw post-offset IK target that was outside this
        # robot's calibrated envelope. The orange `cmd` is the feasible pose;
        # this preserves what was rejected without drawing an impossible pose
        # as though the robot could follow it.
        "ik_rejection": (sink.latest_ik_rejection(max_age_s=0.5)
                         if hasattr(sink, "latest_ik_rejection") else None),
        "hand_backoff": getattr(sink, "last_hand_backoff", None),
        "cam": None,
    }
    if app is not None and hasattr(app, "get"):
        cam = app.get("camera")
        if cam is not None:
            msg["cam"] = cam.latest_ref()
    return msg


async def push_robot_state(ws, sink, hz=30.0, app=None):
    """Send robot_state_message over `ws` at `hz` until the socket closes.

    Its own task, not a tail on the frame handler: when the headset loses
    tracking it stops SENDING frames, and that is exactly when the operator
    most wants to see where the arms are. 30 Hz is enough for an arm the
    onboard slews at a few rad/s and is a third of the frame traffic going
    the other way. A failed send ends this loop only; the receive side of the
    handler notices the close on its own.
    """
    period = 1.0 / float(hz)
    try:
        while not ws.closed:
            message = robot_state_message(sink, app)
            await ws.send_json(message)
            await asyncio.sleep(period)
    except (ConnectionResetError, RuntimeError, OSError) as e:
        print(f"[bridge] robot-state push ended: {e}")


def sent_before(sink):
    """Snapshot before apply(): how many frames the sink had queued so far."""
    return getattr(sink, "n_retargeted", None)


def sent_for_row(sink, before):
    """The arm command the sink queued for THIS frame, or None.

    Read after apply(). The sink drops frames (missing joints, non-finite
    values), and a dropped frame queues nothing -- the previous command is
    still in the sink, and writing it as this row's `sent` would put a command
    on a row the robot never received for it. So `sent` exists only when the
    queue counter moved, and only for a sink that can say what it queued.
    """
    if not hasattr(sink, "commanded"):
        return None
    after = getattr(sink, "n_retargeted", None)
    if before is None or after is None or after == before:
        return None
    return sink.commanded()


def arm_offset_for_row(sink):
    """The sink's current arming offset (versioned), or None."""
    return sink.arm_offset() if hasattr(sink, "arm_offset") else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--retarget", choices=["debug", "g1"], default="debug")
    ap.add_argument("--sink", choices=["log", "sim", "realg1"], default="log")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--world", default="teleop")
    ap.add_argument("--realg1-url", default="", help="onboard commander ws URL for --sink realg1")
    # WebXR needs a SECURE ORIGIN. localhost counts, which is why the USB/adb
    # route worked -- but that cable has been the least reliable part of the
    # chain. With TLS the headset can reach the bridge over wifi at its IP.
    ap.add_argument("--tls-cert", default="")
    ap.add_argument("--tls-key", default="")
    # A LIST. Which high port a headset can actually reach through the office
    # network is not knowable from here -- 8200 gets through and 8443 does not,
    # with no error on either side -- so bind several and let the operator find
    # one rather than redeploying per guess.
    ap.add_argument("--tls-port", default="8443",
                    help="comma-separated ports to serve HTTPS on")
    ap.add_argument("--control-key", default=os.environ.get("TELEOP_CONTROL_KEY", ""),
                    help="require ?key= on POST /control/* (arm, hold, home, ...). "
                         "Unset means anything that can reach this port can arm "
                         "the robot.")
    args = ap.parse_args()

    print(f"[bridge] serving {WEB_DIR}")
    print(f"[bridge] retarget = {args.retarget}, sink = {args.sink}")
    if args.sink == "realg1" and not args.realg1_url:
        ap.error("--sink realg1 needs --realg1-url; without it the sink connects "
                 "to nothing and every frame is silently discarded")
    cfg = arm_mode_from_env()
    prepare_env_for_arm_mode(cfg)          # before the retargeter is built
    retargeter = apply_arm_mode(make_retargeter(args.retarget), cfg)
    smooth_cfg = smoothing_from_env()
    apply_smoothing(retargeter, smooth_cfg)
    if hasattr(retargeter, "set_smooth"):
        print("[bridge] SMOOTH = "
              f"{smooth_cfg['mode']} (cutoff={smooth_cfg['min_cutoff']} Hz, "
              f"beta={smooth_cfg['beta']}, tau={smooth_cfg['tau']} s, "
              f"joint_cost={smooth_cfg['joint_cost']})")
    print(f"[bridge] ARM = {cfg['mode'].upper()}  (TELEOP_ARM=solver|geometry|fingers)")
    if cfg["mode"] == "solver":
        print("[bridge] SOLVER — Unitree's Pinocchio/CasADi IK, all 14 joints from "
              "both wrist poses. "
              + ("Worker thread: fingers at headset rate, arm trails by up to one "
                 "solve (~95-200 ms)." if cfg["async_ik"] else
                 "INLINE: one ~95 ms solve per frame sets the rate for everything, "
                 "fingers included. TELEOP_ASYNC_IK=1 for the worker."))
    elif cfg["mode"] == "fingers":
        make_retargeter_fingers_only(retargeter)
    else:
        if cfg["shoulder"]:
            print("[bridge] SHOULDER — pitch and roll follow the operator too. "
                  "This swings the WHOLE arm.")
            # Yaw joins only when pitch+roll cannot reach inside the robot's
            # calibrated limits (a hand across the body). Off, the wire is the
            # six-joint shape the first hardware runs used.
            if not cfg["shoulder_yaw"]:
                print("[bridge] SHOULDER_YAW=0 — yaw held; a hand across the body "
                      "will roll the upper arm into the torso limit.")
            else:
                import retarget_g1 as _rg
                # The robot's calibrated limits are a deployment fact, installed
                # here rather than at import so the geometry tests see the
                # datasheet closed form they were written against.
                retargeter.ROBOT_LIMITS = _rg.ROBOT_LIMITS
                src = _rg.ROBOT_LIMITS_SOURCE
                print("[bridge] SHOULDER_YAW — yaw moves only when roll cannot reach "
                      f"inside the robot's limits ({'calibrated: ' + src if src else 'DATASHEET ONLY: no calibration file found, set TELEOP_ARM_CAL'})")
            # On with the shoulder, off only by asking. A driven shoulder
            # rotates the forearm frame the wrist joints act in; solving the
            # wrist against a constant frame then tilts the hand by the whole
            # shoulder motion (up to 108 deg of roll on a recorded session).
            if cfg["wrist_fk"]:
                print("[bridge] WRIST_FK — wrist solved against the forearm's "
                      "real orientation, relative to the zero pose "
                      "(0.0000 deg against ~86 for the constant). "
                      "TELEOP_WRIST_FK=0 disables it.")
            else:
                print("[bridge] WRIST_FK=0 — wrist conjugated by a constant; "
                      "expect the hand to tilt as the shoulder moves.")
        print("[bridge] GEOMETRY — elbow from reach, wrists from orientation"
              + (", shoulder from direction" if cfg["shoulder"] else "; shoulder held")
              + ". No arm IK. Signs: POST /wristcal/flip/{side}/{roll|pitch|yaw}")
    sink = make_sink(args.sink, args.world, args.realg1_url)
    app = make_app(retargeter, sink)
    from control_events import ControlEvents
    events_dir = (app["session"].dir if app.get("session") is not None
                  else SessionRecorder.pick_dir(os.environ.get("TELEOP_SESSION_DIR", "")))
    app["control_events"] = ControlEvents(path=os.path.join(events_dir, "control-events.jsonl"))
    sink.control_events = app["control_events"]
    app["control_events"].emit("bridge.started", commit=build_info().get("commit"))
    app["arm_mode"] = cfg["mode"]
    # The robot's head camera, written beside every session when recording
    # is on, and shown in the headset (/camera/frame.jpg) either way.
    camera = None
    session_on_frame = (app["session"].note_camera
                        if app.get("session") is not None else None)
    head_camera = os.environ.get("TELEOP_HEAD_CAMERA", "").strip().lower()
    if (args.sink == "realg1" and os.environ.get("TELEOP_CAM", "1") not in ("0", "off")
            and head_camera not in ("none", "off")):
        from cam_client import CameraClient, CameraServiceClient, cam_url_from_ctrl
        # TELEOP_CAM_SOURCE=camera (or direct) pulls JPEGs from a separate
        # camera service at TELEOP_CAMERA_URL; otherwise the onboard's own
        # /cam websocket (real/g1_camera.py) is the source.
        want = os.environ.get("TELEOP_CAM_SOURCE", "").strip().lower()
        camera_url = os.environ.get("TELEOP_CAMERA_URL", "").strip()
        if want in ("camera", "direct"):
            if not camera_url:
                raise ValueError("TELEOP_CAM_SOURCE=camera requires TELEOP_CAMERA_URL")
            camera = CameraServiceClient(camera_url, on_frame=session_on_frame)
            print(f"[bridge] CAMERA — persistent direct JPEG stream from "
                  f"{camera_url}/frame.jpg")
        else:
            camera = CameraClient(cam_url_from_ctrl(args.realg1_url),
                                  on_frame=session_on_frame)
            print("[bridge] CAMERA — head camera frames from the onboard's /cam"
                  + (" go beside each session as <sid>.frames/" if session_on_frame else "")
                  + " (TELEOP_CAM=0 to disable)")
    app["camera"] = camera
    # The hand cameras (cameras/hand_cameras.py), recorded beside each session
    # as <sid>.frames/hand-<name>/. Only when recording: the console's live
    # view goes through /hand-camera/* and needs no stream here.
    if (args.sink == "realg1" and session_on_frame is not None
            and hand_cams_enabled()):
        from cam_client import CameraClient
        base = _hand_cam_base().replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        session = app["session"]
        app["hand_cams"] = {
            name: CameraClient(f"{base}/cam/{name}",
                               on_frame=(lambda header, jpeg, name=name:
                                         session.note_hand_camera(name, header, jpeg)),
                               label=f"hand-{name}")
            for name in HAND_CAM_NAMES}
        print(f"[bridge] HAND CAMERAS — recorded beside each session as "
              f"<sid>.frames/hand-left/ and hand-right/ (from {base})")
    # thumb_0 across the palm at a full squeeze: the operator's finding, kept
    # across redeploys. Refuses a bad value rather than gripping wrong.
    v = grip_thumb0_from_env()
    if v is not None:
        print(f"[bridge] grip thumb0 = {v} (TELEOP_GRIP_THUMB0)")
    app["task"] = task_from_env()
    if app["task"]:
        print(f"[bridge] TASK — {app['task']!r}; every session header carries it")
    elif app.get("session") is not None:
        print("[bridge] no task: sessions record task: null "
              "(set TELEOP_TASK, or /session/start?task=)")
    app["control_key"] = args.control_key
    if args.control_key:
        print("[bridge] /control/* requires ?key=")
    else:
        print("[bridge] WARNING: /control/* is UNAUTHENTICATED — anything that "
              "can reach this port can arm the robot. Set TELEOP_CONTROL_KEY.")
    tls = None
    if args.tls_cert and args.tls_key and os.path.exists(args.tls_cert):
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(args.tls_cert, args.tls_key)
    app["lan_https_required"] = tls is not None
    tls_ports = [int(x) for x in str(args.tls_port).split(",") if x.strip()]
    if tls is not None:
        print(f"[bridge] https on {tls_ports} — open ONE of these on the Quest, "
              f"accept the certificate warning once")
    print(f"[bridge] http  on :{args.port} (no WebXR unless reached as localhost)")
    asyncio.run(_serve(app, args.port, tls, tls_ports))


async def _serve(app, port, tls, tls_ports):
    """Run plain HTTP and, if configured, HTTPS on the same app.

    Both are kept: the control page and /control/status are plain HTTP so a
    phone can reach them without a certificate dance, while the headset needs
    the TLS origin to get WebXR at all.
    """
    runner = web.AppRunner(app)
    await runner.setup()
    # USB/adb laptop sessions use loopback so the hotel LAN cannot reach the
    # operator controls. Keep the device deployment's LAN binding by default.
    bind_host = os.environ.get("TELEOP_BIND_HOST", "0.0.0.0")
    await web.TCPSite(runner, bind_host, port).start()
    for tp in (tls_ports if tls is not None else ()):
        try:
            await web.TCPSite(runner, os.environ.get("TELEOP_TLS_BIND_HOST", bind_host),
                              tp, ssl_context=tls).start()
        except OSError as e:
            # One port already taken must not cost us the others.
            print(f"[bridge] could not bind https on :{tp}: {e}")
            if app.get("lan_https_required"):
                await runner.cleanup()
                raise
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    main()
