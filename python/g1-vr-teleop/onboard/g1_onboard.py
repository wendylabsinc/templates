#!/usr/bin/env python3
"""G1 onboard teleop commander — the Wendy app deployed ON the robot.

Receives pre-retargeted upper-body joint targets from the laptop bridge over a
WebSocket and drives the real G1 via Unitree DDS: arms through `rt/arm_sdk`
(balance-safe — the onboard controller keeps the robot upright, we command only
the 14 arm joints) and the Dex3-1 hands through `rt/dex3/*/cmd`.

SAFETY STATE MACHINE (operator-commanded, never automatic):
    STANDBY   only reads robot state; does NOT touch the arms.
    HOLDING   ({"cmd":"take_control"}) grabs the arms via arm_sdk and holds the
              current pose. No teleop motion yet. Speed ramp armed (5 s).
    ARMED     ({"cmd":"arm"}) applies incoming targets. Dead-man watchdog: if no
              frame for >0.3 s, freeze at the current pose.
    ({"cmd":"relax"} / disconnect / Ctrl-C) -> go_home, release arm_sdk, relax hands.

Requires unitree_sdk2py + aiohttp on the robot's onboard computer.
NOT yet run on hardware — review before arming, and keep an e-stop in reach.

    python3 g1_onboard.py --net eth0 --port 8090      # STANDBY; operator arms via UI
"""

import argparse
import asyncio
import json
import collections
import math
import os
import queue
import secrets
import sys
import threading
import time

from arm_reference import ArmReference, validate_motion_limits

import numpy as np
from aiohttp import web, WSMsgType

# Pure Python beside this file: the Dex3 closed pose, range table and the
# stall back-off. No DDS import, so it is exercised by the same tests as the
# rest of the safety logic here.
from dex3_stall import StallBackoff, sanitize_hand_pose, CLOSED_LEFT, CLOSED_RIGHT

# unitree_sdk2py is imported inside main(), not here, so that the validation and
# state-machine logic in this file can be imported and unit-tested on a machine
# with no DDS stack. Safety code that can only be exercised on a robot does not
# get exercised.

DEAD_MAN_S = 1.0           # begin slow return home after this input-loss window
# 0.3 s was too tight for real hand tracking: the Quest loses both hands for
# longer than that routinely, so every dropout forced a manual re-arm -- observed
# as ARMED for two seconds, 225 frames applied, then frozen on a 0.32 s gap.
#
# Relaxing it does NOT let the arms move more. When frames stop, apply() is not
# called, the controller keeps commanding its last target, and the arm HOLDS.
# The watchdog only decides whether a human must re-arm. The sink already
# refuses to re-send stale poses, so a gap means no commands rather than old
# ones, and the slew limit caps the catch-up when tracking returns.
ARM_DIM = 14
HAND_DIM = 7
# The measured BODY slice carried in every reply: Unitree motor indices 12..28
# -- waist yaw/roll/pitch (12, 13, 14) then the 14 arm joints (15..28), in
# Unitree order. Not just the arm, and not just q: a physics replay of a
# recorded command stream (the direct way to measure the sim-to-real gap)
# needs velocities, and the simulator's 43-joint model moves the waist.
# `measured_arm_q` remains the arm-only 14-vector everything already reads.
BODY_IDX = list(range(12, 29))
# All active motors in the 29-DoF G1 body. Keep this separate from BODY_IDX:
# robot.body is the waist+arms physics-replay contract, while temperature is
# useful for every leg, waist and arm motor even though teleop does not command
# the legs.
MOTOR_TEMPERATURE_IDX = list(range(29))
MOTOR_TEMPERATURE_NAMES = [
    "left_hip_pitch", "left_hip_roll", "left_hip_yaw", "left_knee",
    "left_ankle_pitch", "left_ankle_roll", "right_hip_pitch",
    "right_hip_roll", "right_hip_yaw", "right_knee", "right_ankle_pitch",
    "right_ankle_roll", "waist_yaw", "waist_roll", "waist_pitch",
    "left_shoulder_pitch", "left_shoulder_roll", "left_shoulder_yaw",
    "left_elbow", "left_wrist_roll", "left_wrist_pitch", "left_wrist_yaw",
    "right_shoulder_pitch", "right_shoulder_roll", "right_shoulder_yaw",
    "right_elbow", "right_wrist_roll", "right_wrist_pitch", "right_wrist_yaw",
]
# How close a joint must be to home for the move to count as arrived. Larger
# than the steady-state droop a held arm shows (2-3 deg measured) and far
# smaller than any pose confusion worth reporting.
HOME_ARRIVED_RAD = 0.105        # ~6 deg
MAX_FRAME_HZ = 200.0       # websocket mailbox waits; direct flooding calls refuse
TARGET_MAX_AGE_S = 0.15    # local queue residence, matching bridge input freshness
# Dex3 finger stiffness. 1.5 is Unitree's teleop default and it is what the
# hands have always run. With the fingers finally reporting back, measured on
# the right hand against a full trigger sweep:
#
#   middle_1, index_1   commanded 0..100 deg, moved 0..98   -- follow
#   middle_0            commanded 0..90,      moved 11..16  -- 4.7 of 90
#   index_0             commanded 0..90,      moved -4..47  -- half
#
# The operator's words were "knuckles don't move", and it was torque. Measured
# on the right hand at kp=3.0, same full trigger sweep, nothing in the hand:
#
#   middle_0   commanded 0..90 deg, moved 0..88    99 %   (was 5 %)
#   index_0    commanded 0..90,     moved -2..88  100 %   (was 52 %)
#   thumb_2    commanded -100..0,   moved -96..0   96 %   (was stuck)
#
# So 3.0 is the default now: 1.5 is Unitree's teleop figure and it cannot
# close this hand's knuckles, which is not a preference to be remembered on
# a deploy command line. G1_DEX3_KP still overrides it.
DEX3_KP = float(os.environ.get("G1_DEX3_KP", "3.0"))
DEX3_KD = float(os.environ.get("G1_DEX3_KD", "0.2"))

# The G1's own remote, read from rt/lowstate ON THE ROBOT, is the dead-man.
#
# This is the best place for it by some distance: it is independent of the
# headset, the browser, the WebSocket, the laptop and the network. Nothing in
# the teleop chain can enable motion -- only a hand on that remote, a few
# microseconds before the motors. A stalled bridge, a wedged browser or a
# dropped link cannot hold it down.
#
# Bit layout of wireless_remote[2:4] as a little-endian uint16. VERIFY on the
# robot with --deadman-discover before trusting any of these names: the layout
# is taken from Unitree's SDK examples, not measured, and this file's own
# history is a lesson in what assumed index maps cost.
REMOTE_BITS = {
    "R1": 0, "L1": 1, "start": 2, "select": 3,
    "R2": 4, "L2": 5, "F1": 6, "F2": 7,
    "A": 8, "B": 9, "X": 10, "Y": 11,
    "up": 12, "right": 13, "down": 14, "left": 15,
}
REMOTE_STALE_S = 0.5       # no lowstate this long => treat the remote as released
LOWSTATE_STALE_S = 1.0     # no lowstate this long => the robot is not talking to us
RX_REQUIRED_S = 0.5        # hand data must be this fresh before arming is allowed
# Process 1 in N lowstate samples. Was 10, when a DDS listener handed this
# callback every published sample (~300/s reached it) and 9 in 10 were decoded
# for nothing. The LowstateHub (lowstate_hub.py) now polls the newest sample at
# G1_LOWSTATE_HZ (200), so every sample it takes is worth processing: the leash
# and the recordings get a measurement at most ~5 ms old instead of ~30 Hz in
# bursts.
LOWSTATE_DECIMATE = 1

# How far the COMMAND may diverge from where the arm actually is.
#
# A different failure from the slew limit, and neither covers the other.
# ARM_SLEW_RAD_S bounds how fast the command CHANGES; a blocked, stalled or
# overloaded joint satisfies that forever while the gap between what we asked
# for and what the arm did grows without bound. _slew chains off self._last_cmd
# and touches measurement only to SEED -- at arming and after a freeze -- so
# between those the command can walk arbitrarily far from reality at 2.5 rad/s.
#
# Clamping rather than faulting is deliberate: a fault drops a demo, a clamp
# degrades into "lags where it is stuck, tracks everywhere else", and the
# counter names the joint. Fault later if the data asks for it.
#
# 10 deg is DimOS's default. It was set for a measurement ~33 ms stale (4.7 deg
# of legitimate lag at full slew). Under GIL load on 2026-09-19 the measurement
# arrived 110-145 ms apart, and the leash fired on 41 % of applied frames with
# torques far too low for a real 10 deg error: it was clamping to a stale
# reading, pulling the command back, and letting it go again 2-5 times a second.
#
# So the band widens by a bounded estimate of what the arm can legitimately
# have moved since the measurement was taken. This rate is deliberately
# independent of the operator-tunable smoothing ceiling: increasing command
# speed must not silently loosen the blocked-joint guard. 3.5 rad/s preserves
# the allowance used by the last physically exercised working version. A
# genuinely blocked joint still grows its error without bound and is clamped;
# a reading older than the age cap is LOWSTATE_STALE_S's and the watchdog's
# problem, not the leash's.
MAX_TRACKING_ERROR_RAD = math.radians(10.0)
LEASH_AGE_CAP_S = 0.2
LEASH_AGE_RATE_RAD_S = 3.5
HOME_SLEW_RAD_S = 0.6      # deliberately slower than teleop; this runs unattended


def input_loss_policy():
    """Expose the running policy so preflight cannot mistake old freeze builds."""
    return {"version": 1, "mode": "return_home", "timeout_s": DEAD_MAN_S,
            "home_slew_rad_s": HOME_SLEW_RAD_S, "automatic_rearm": False}

# Per-joint stiffness, settable from the jog panel. Bounds are the whole
# safety story for gains: kp is capped at roughly twice the vendored
# controller's arm default (80) on the shoulder and elbow and at twice the
# wrist default (40) on the wrists; kd at 5. A change is RAMPED over
# GAIN_RAMP_S rather than stepped -- a stiffness step at a standing position
# error is a torque step.
GAIN_KP_MAX = {"shoulder": 150.0, "elbow": 150.0, "wrist": 80.0}
GAIN_KD_MAX = 5.0
GAIN_RAMP_S = 0.5
GAIN_RAMP_STEPS = 25
# How often the onboard volunteers its measured joints while nobody is
# sending frames, so a jog panel shows a live arm in HOLDING.
IDLE_Q_HZ = 20.0
# The Dex3 command goes out on every frame. With no hands fitted it is pure
# cost, and it sits on the same path as the arm command.
SEND_HANDS = os.environ.get("G1_SEND_HANDS", "1") != "0"
# /state is a Wendy persist entitlement (see real/wendy.json), so it is a
# directory on the DEVICE, not in the container. /tmp was inside the container
# and a redeploy replaces the container -- so the first captured home pose was
# silently lost on the next deploy, having been reported as persistent.
STATE_DIR = os.environ.get("G1_STATE_DIR", "/state")
HOME_PATH = os.environ.get("G1_HOME_FILE", os.path.join(STATE_DIR, "home.json"))
MOTION_LIMITS_PATH = os.path.join(STATE_DIR, "motion-limits.json")
GAINS_PATH = os.environ.get("G1_GAINS_FILE", os.path.join(STATE_DIR, "gains.json"))

# Fixed phrases only. The bridge sends one of these identifiers rather than
# arbitrary text, so the robot's speech surface cannot be turned into an
# unbounded remote TTS endpoint. Unitree's official G1 audio example uses
# speaker_id 1 for English.
SPOKEN_PROMPTS = {
    "drink_question": "What would you like to drink?",
    "thank_you": "Thank you!",
}
TTS_ENGLISH_SPEAKER_ID = 1

# Joint limits for the G1's 14 arm joints, in the order ctrl_dual_arm expects
# (left shoulder p/r/y, elbow, wrist r/p/y, then the same for the right).
# From the Unitree MJCF; roll is mirrored between arms, which is why the two
# halves are not identical. This is the LAST line of defence: a retargeter bug,
# a mis-scaled unit or a half-decoded frame stops here rather than at a stop.
# Per-joint slew limit on commanded arm targets, rad/s.
#
# The vendored controller already clips velocity, but at 30 rad/s (~1720 deg/s),
# which does not protect the moment that actually needs protecting: the FIRST frame
# after arming, which steps the arms from wherever they are to wherever the
# operator's hands are. That gap can be a metre.
#
# The default is shared with the motor-clock reference. The commander uses
# its live, persisted speed cap for the upstream slew so the two agree.
ARM_SLEW_RAD_S = ArmReference.max_velocity
# The waist turns the whole upper body -- both arms, the hands, and whatever
# they hold -- and the balance controller under rt/arm_sdk has to follow the
# shift. Slower than the arms by design: a turn is a repositioning, not
# a reach, and 0.6 rad/s (~34 deg/s) is a brisk turn for a person carrying
# something.
WAIST_SLEW_RAD_S = 0.6
WAIST_VELOCITY_MAX_RAD_S = WAIST_SLEW_RAD_S
WAIST_MOTOR = 12                       # Unitree G1 waist yaw; roll/pitch never
# The model says +-2.618 rad (assets/g1/g1_body29_hand14.xml); the default
# here is that less a few degrees, so a held stick stops short of the
# mechanical stop rather than stalling against it. It was +-1.5 while nobody
# had turned this waist at all; the operator asked for the whole turn.
# G1_WAIST_MAX_RAD lowers it again for a cautious first run without a rebuild,
# and a `waist_yaw: [lo, hi]` in the calibration file overrides both.
WAIST_MODEL_MAX_RAD = 2.618
WAIST_MAX_RAD_DEFAULT = 2.5


def _waist_max_from_env(env=None):
    """G1_WAIST_MAX_RAD, kept positive and inside the model. Bad value -> default."""
    env = os.environ if env is None else env
    raw = env.get("G1_WAIST_MAX_RAD")
    if raw in (None, ""):
        return WAIST_MAX_RAD_DEFAULT
    try:
        v = abs(float(raw))
    except (TypeError, ValueError):
        print(f"[waist] G1_WAIST_MAX_RAD={raw!r} is not a number; "
              f"using {WAIST_MAX_RAD_DEFAULT} rad")
        return WAIST_MAX_RAD_DEFAULT
    if not math.isfinite(v) or v <= 0:
        print(f"[waist] G1_WAIST_MAX_RAD={raw!r} is not a usable limit; "
              f"using {WAIST_MAX_RAD_DEFAULT} rad")
        return WAIST_MAX_RAD_DEFAULT
    return min(v, WAIST_MODEL_MAX_RAD)


_WAIST_MAX = _waist_max_from_env()
WAIST_LIMIT_DEFAULT = (-_WAIST_MAX, _WAIST_MAX)

# URDF joint names in the order of the 14-vector, so a partial frame can name
# exactly which joints it drives. Forearm mode sends only elbow + wrist; the
# shoulder never appears, and what never appears can never be commanded.
ARM_JOINT_ORDER = [
    "left_shoulder_pitch_joint", "left_shoulder_roll_joint",
    "left_shoulder_yaw_joint", "left_elbow_joint", "left_wrist_roll_joint",
    "left_wrist_pitch_joint", "left_wrist_yaw_joint",
    "right_shoulder_pitch_joint", "right_shoulder_roll_joint",
    "right_shoulder_yaw_joint", "right_elbow_joint", "right_wrist_roll_joint",
    "right_wrist_pitch_joint", "right_wrist_yaw_joint",
]
ARM_JOINT_INDEX = {n: i for i, n in enumerate(ARM_JOINT_ORDER)}

# Joint order of the 14-vector everything here speaks, for readable diagnostics.
ARM_JOINT_NAMES = [
    "L_shoulder_pitch", "L_shoulder_roll", "L_shoulder_yaw", "L_elbow",
    "L_wrist_roll", "L_wrist_pitch", "L_wrist_yaw",
    "R_shoulder_pitch", "R_shoulder_roll", "R_shoulder_yaw", "R_elbow",
    "R_wrist_roll", "R_wrist_pitch", "R_wrist_yaw",
]

ARM_LIMITS = [
    (-3.0892, 2.6704), (-1.5882, 2.2515), (-2.618, 2.618), (-1.0472, 2.0944),
    (-1.9722, 1.9722), (-1.6144, 1.6144), (-1.6144, 1.6144),
    (-3.0892, 2.6704), (-2.2515, 1.5882), (-2.618, 2.618), (-1.0472, 2.0944),
    (-1.9722, 1.9722), (-1.6144, 1.6144), (-1.6144, 1.6144),
]


# Dex3-1 joint limits, read from the vendored URDFs (assets/unitree_hand/
# unitree_dex3_{left,right}.urdf), in DEX3 motor order:
#   thumb_0, thumb_1, thumb_2, middle_0, middle_1, index_0, index_1
#
# They are MIRRORED between hands -- left middle_0 is [-1.571, 0] where right is
# [0, 1.571] -- so a single shared table would drive one hand's fingers straight
# into their stops. Until this, sanitize_hand clamped nothing at all: finger
# targets were checked for finiteness and passed to the motors unbounded, while
# every other path in this file validated its range.
DEX3_LIMITS = {
    "left": [
        (-1.04719755, 1.04719755), (-0.72431163, 0.920), (0.0, 1.74532925),
        (-1.57079632, 0.0), (-1.74532925, 0.0),
        (-1.57079632, 0.0), (-1.74532925, 0.0),
    ],
    "right": [
        (-1.04719755, 1.04719755), (-0.920, 0.72431163), (-1.74532925, 0.0),
        (0.0, 1.57079632), (0.0, 1.74532925),
        (0.0, 1.57079632), (0.0, 1.74532925),
    ],
}

# Calibration: the ARM_LIMITS above are the mechanical datasheet range (shoulder
# pitch -177 to +153 deg). They say nothing about what is safe on THIS robot --
# self-collision, the Dex3 hands adding length, cables binding early. A
# calibration pass records what the arms were actually walked through by hand.
CAL_PATH = os.environ.get("G1_CAL_FILE", os.path.join(STATE_DIR, "arm_cal.json"))
CAL_MARGIN_RAD = 0.05      # back off from each observed extreme
CAL_MIN_TRAVEL = 0.15      # below this a joint counts as "not exercised"


# A calibration shipped in the image, so a wiped volume or a fresh robot does
# not mean re-walking every joint by hand.
CAL_BUNDLED = os.environ.get(
    "G1_CAL_BUNDLED",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "calibration", "unitree-g1-nx-2.json"))


def _read_cal(path):
    """-> (margined limits, raw observed extremes or None)."""
    try:
        with open(path) as fh:
            blob = json.load(fh)
        lim = blob["limits"]
        if len(lim) != ARM_DIM:
            return None, None
        limits = [(float(a), float(b)) for a, b in lim]
    except (OSError, ValueError, KeyError, TypeError):
        return None, None
    # The RAW extremes are kept alongside the margined limits so a later pass
    # can extend them. Seeding a top-up from the margined values instead would
    # re-apply CAL_MARGIN_RAD every time, shrinking the envelope on each run.
    obs = blob.get("observed")
    if isinstance(obs, dict) and len(obs.get("lo", [])) == ARM_DIM:
        return limits, {"lo": [float(v) for v in obs["lo"]],
                        "hi": [float(v) for v in obs["hi"]]}
    return limits, None


def _load_cal():
    limits, _ = _read_cal(CAL_PATH)
    if limits is None and os.path.isfile(CAL_BUNDLED):
        limits, _ = _read_cal(CAL_BUNDLED)
        if limits is not None:
            print(f"[onboard] using bundled calibration {CAL_BUNDLED}")
    return limits


def _load_cal_observed():
    _, obs = _read_cal(CAL_PATH)
    return obs


ARM_LIMITS_CAL = _load_cal()


def effective_arm_limits():
    """Calibrated limits if we have them, else the mechanical ones."""
    return ARM_LIMITS_CAL or ARM_LIMITS


def _load_waist_cal():
    """`waist_yaw: [lo, hi]` from the calibration file, if it has one."""
    for path in (CAL_PATH, CAL_BUNDLED):
        try:
            with open(path) as fh:
                lim = json.load(fh).get("waist_yaw")
            lo, hi = float(lim[0]), float(lim[1])
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            continue
        if math.isfinite(lo) and math.isfinite(hi) and lo < hi:
            return (max(lo, -2.618), min(hi, 2.618))
    return None


WAIST_LIMITS_CAL = _load_waist_cal()


def effective_waist_limits():
    """Calibrated waist yaw limits if the file names them, else the narrowed default."""
    return WAIST_LIMITS_CAL or WAIST_LIMIT_DEFAULT


def sanitize_waist(value):
    """-> a finite, limited waist yaw in radians, or raise. Sets LAST_WAIST_CLAMPED."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"waist must be a number, got {type(value).__name__}")
    f = float(value)
    if not math.isfinite(f):
        raise ValueError(f"waist is not finite: {value!r}")
    lo, hi = effective_waist_limits()
    c = min(hi, max(lo, f))
    LAST_WAIST_CLAMPED["clamped"] = c != f
    return c


LAST_WAIST_CLAMPED = {"clamped": False}


def sanitize_waist_velocity(value):
    """Finite signed speed, bounded independently from the bridge."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"waist velocity must be a number, got {type(value).__name__}")
    velocity = float(value)
    if not math.isfinite(velocity):
        raise ValueError(f"waist velocity is not finite: {value!r}")
    return min(WAIST_VELOCITY_MAX_RAD_S,
               max(-WAIST_VELOCITY_MAX_RAD_S, velocity))


def _reject_nonfinite(value):
    """json.loads accepts bare NaN and Infinity by default. A NaN target on a
    position-controlled joint is undefined behaviour, so refuse it at the door
    rather than clamping it -- a clamped NaN is how the sibling repo turned a
    bad frame into a full-scale command."""
    raise ValueError(f"non-finite value in frame: {value!r}")


# How many joints the last frame had clamped to a limit. A mapping that aims
# outside the arm's reach shows up here as most of the vector pinned to the
# bounds every frame -- which is what a retargeter aiming 0.84 m below the
# robot's feet looked like, while every other indicator stayed green.
LAST_CLAMPED = {"n": 0, "joints": []}


def sanitize_arm(values):
    """-> a finite, joint-limited 14-vector, or raise."""
    if not isinstance(values, (list, tuple)) or len(values) != ARM_DIM:
        raise ValueError(f"arm must be {ARM_DIM} numbers, got {type(values).__name__}")
    out, clamped = [], []
    for i, v in enumerate(values):
        f = float(v)
        if not math.isfinite(f):
            raise ValueError(f"arm[{i}] is not finite: {v!r}")
        lo, hi = effective_arm_limits()[i]
        c = min(hi, max(lo, f))
        if c != f:
            clamped.append(i)
        out.append(c)
    LAST_CLAMPED["n"], LAST_CLAMPED["joints"] = len(clamped), clamped
    return out


def sanitize_hand(values, side):
    """-> a finite, joint-limited 7-vector for one hand, or raise."""
    if side not in DEX3_LIMITS:
        raise ValueError(f"hand side must be left or right, got {side!r}")
    if not isinstance(values, (list, tuple)) or len(values) != HAND_DIM:
        raise ValueError(f"hand must be {HAND_DIM} numbers")
    out = []
    for i, v in enumerate(values):
        f = float(v)
        if not math.isfinite(f):
            raise ValueError(f"hand value is not finite: {v!r}")
        lo, hi = DEX3_LIMITS[side][i]
        out.append(min(hi, max(lo, f)))
    return out


def _load_home():
    """Home survives a redeploy; a pose you captured should not evaporate."""
    try:
        with open(HOME_PATH) as fh:
            q = json.load(fh)["arm"]
        return [float(v) for v in q] if len(q) == ARM_DIM else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _load_gains():
    try:
        with open(GAINS_PATH) as fh:
            g = json.load(fh)["gains"]
        if len(g) != ARM_DIM:
            return None
        return [(float(a), float(b)) for a, b in g]
    except (OSError, ValueError, KeyError, TypeError):
        return None


_ARM_NAME_BY_INDEX = {i: n for n, i in ARM_JOINT_INDEX.items()}


def leash_band(measured_t, now_mono):
    """The leash's half-width, rad: MAX_TRACKING_ERROR_RAD plus what the arm
    can have moved since the measurement was taken (see the constant).

    `measured_t` None -- no timestamp, as in a Commander built without
    __init__ -- counts as fresh: the band is exactly MAX_TRACKING_ERROR_RAD.
    """
    if measured_t is None:
        return MAX_TRACKING_ERROR_RAD
    age = min(max(0.0, now_mono - measured_t), LEASH_AGE_CAP_S)
    return MAX_TRACKING_ERROR_RAD + LEASH_AGE_RATE_RAD_S * age


def _body_slice(msg, now_mono):
    """BODY_IDX q/dq/tau_est and the lowstate tick from one message, or None.

    q and dq are required -- without them there is no body state worth
    sending. tau_est and tick are taken when the IDL has them and recorded as
    None when it does not, rather than failing the slice: an older message
    layout must cost the torque column, not the velocities.
    """
    try:
        ms = msg.motor_state
        q = [float(ms[i].q) for i in BODY_IDX]
        dq = [float(ms[i].dq) for i in BODY_IDX]
    except (AttributeError, IndexError, TypeError, ValueError):
        return None
    try:
        tau = [float(ms[i].tau_est) for i in BODY_IDX]
    except (AttributeError, IndexError, TypeError, ValueError):
        tau = None
    try:
        temperature = []
        for i in BODY_IDX:
            value = ms[i].temperature
            # Some Unitree IDLs expose one sensor and others expose a pair.
            if isinstance(value, (list, tuple)):
                value = max(value)
            temperature.append(float(value))
    except (AttributeError, IndexError, TypeError, ValueError):
        temperature = None
    tick = getattr(msg, "tick", None)
    try:
        tick = int(tick) if tick is not None else None
    except (TypeError, ValueError):
        tick = None
    return {"idx": list(BODY_IDX), "q": q, "dq": dq, "tau": tau,
            "temperature": temperature, "tick": tick, "t": now_mono}


def _body_message(body):
    """The wire form of measured_body: rounded, with its age on this clock.

    5 dp on radians and rad/s is a thousandth of a degree; 3 dp on N m is
    below the estimate's own noise. 17 x 3 floats per reply at 50 Hz is ~2 kB/s.
    """
    if not body:
        return None
    return {"idx": body["idx"],
            "q": [round(v, 5) for v in body["q"]],
            "dq": [round(v, 5) for v in body["dq"]],
            "tau": ([round(v, 3) for v in body["tau"]] if body.get("tau") is not None else None),
            "temperature": ([round(v, 1) for v in body["temperature"]]
                            if body.get("temperature") is not None else None),
            "tick": body.get("tick"),
            "age_s": round(time.monotonic() - body["t"], 4)}


def _motor_temperature_value(motor):
    """Return the hottest reported sensor for one body motor, or None."""
    try:
        value = motor.temperature
    except AttributeError:
        return None
    if isinstance(value, (list, tuple)):
        values = []
        for item in value:
            try:
                item = float(item)
            except (TypeError, ValueError):
                continue
            if math.isfinite(item):
                values.append(item)
        return max(values) if values else None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _motor_temperature_slice(msg, now_mono):
    """Temperature for all 29 active G1 body motors from one lowstate."""
    try:
        motors = msg.motor_state
        values = [_motor_temperature_value(motors[i])
                  for i in MOTOR_TEMPERATURE_IDX]
    except (AttributeError, IndexError, TypeError):
        return None
    if not any(value is not None for value in values):
        return None
    tick = getattr(msg, "tick", None)
    try:
        tick = int(tick) if tick is not None else None
    except (TypeError, ValueError):
        tick = None
    return {"idx": list(MOTOR_TEMPERATURE_IDX),
            "names": list(MOTOR_TEMPERATURE_NAMES),
            "temperature_c": values, "tick": tick, "t": now_mono}


def _motor_temperature_message(temperature):
    """Wire form plus a compact hottest-motor summary."""
    if not temperature:
        return None
    values = temperature["temperature_c"]
    numeric = [v for v in values if v is not None]
    max_c = max(numeric) if numeric else None
    hottest = ([name for name, value in zip(temperature["names"], values)
                if value is not None and value == max_c] if max_c is not None else [])
    return {"idx": temperature["idx"], "names": temperature["names"],
            "temperature_c": values, "tick": temperature.get("tick"),
            "age_s": round(time.monotonic() - temperature["t"], 4),
            "max_c": max_c, "hottest": hottest}


_NO_REFUSAL_OVERRIDE = object()


def ack_message(cmd, seq, applied, clamped_idx=None,
                refused_override=_NO_REFUSAL_OVERRIDE):
    """What the onboard says back for every pose frame it read.

    `q` is the measured 14-vector, so the bridge -- and the episode recorder
    behind it -- get the arm's real state at frame rate instead of the 1 Hz
    status poll. `applied` separates "read" from "commanded": a frame refused
    by the leash, the rate limit or a non-ARMED state is acked False.

    `clamped` names the joints this frame's arm command was clamped to a
    limit, `leashed` the joints the tracking leash held back. Both were
    already counted for /status, as totals. Per frame they answer the question
    the first full-stack episode cost an evening: 58 % of the right shoulder
    roll commands were past the calibrated limit, and nothing in the recording
    said so.

    `clamped_idx` is captured by the caller right after sanitising THIS
    frame's arm, and is None for a fingers-only frame, whose ack must not
    inherit the clamp list of the last arm frame.
    """
    with cmd.lock:
        q = cmd.measured_arm_q
        body = getattr(cmd, "measured_body", None)
        motor_temperature = getattr(cmd, "measured_motor_temperature", None)
        measured_t = getattr(cmd, "measured_arm_t", None)
        leashed = list(getattr(cmd, "_leash_hit", None) or []) if applied else []
    # Bridge-side joint names (left_elbow_joint), the same ones the recorded
    # targets use, so replay can line a clamp up with the command that caused it.
    clamped = [_ARM_NAME_BY_INDEX[i] for i in (clamped_idx or [])]
    # Measured FINGER positions, in the bridge's joint order. The hands were
    # open loop from the start: the state topics were imported and never
    # subscribed, so a finger stopped by an object looked exactly like one
    # closing freely and no episode carried any finger measurement at all.
    hands = None
    h = getattr(cmd, "hand", None)
    if h is not None and hasattr(h, "measured"):
        try:
            m = h.measured()
            hands = {k: ([round(float(x), 5) for x in v] if v else None)
                     for k, v in m.items()}
        except Exception:  # noqa: BLE001 -- feedback must never cost a frame
            hands = None
    refused = (getattr(cmd, "last_refusal", None)
               if refused_override is _NO_REFUSAL_OVERRIDE else refused_override)
    return {"ack": seq, "state": cmd.state, "applied": bool(applied),
            "homing": getattr(cmd, "homing", False),
            "input_loss_recovery": getattr(cmd, "input_loss_recovery", None),
            "arm_reference": _arm_reference_message(cmd),
            "q": [round(float(v), 5) for v in q] if q is not None else None,
            "q_age_s": (max(0.0, time.monotonic() - measured_t)
                        if measured_t is not None else None),
            # Waist + arms with velocities and torque, for a physics replay.
            # `q` above stays the arm-only 14-vector every reader expects.
            "body": _body_message(body),
            "motor_temperature": _motor_temperature_message(motor_temperature),
            # Motor 12, once a waist was commanded this hold; None before.
            "waist": getattr(cmd, "last_waist", None),
            "clamped": clamped, "leashed": leashed, "hands": hands,
            # Temperature, current, mode and error per Dex3 motor -- what says
            # whether a knuckle that stopped following is hot or latched.
            "hand_state": _hand_state_message(h),
            # Finger joints this frame's command was backed off as stalled.
            "hand_backoff": dict(getattr(cmd, "_hand_backoff_names", None)
                                 or {"left": [], "right": []}),
            "refused": None if applied else refused}


def _hand_state_message(hand):
    """Dex3Sender.measured_state(), rounded for the wire, or None."""
    if hand is None or not hasattr(hand, "measured_state"):
        return None
    try:
        st = hand.measured_state()
    except Exception:  # noqa: BLE001 -- feedback must never cost a frame
        return None
    if not isinstance(st, dict):
        return None
    out = {}
    for side, s in st.items():
        if not isinstance(s, dict):
            out[side] = None
            continue
        row = {}
        for k in ("q", "dq", "tau_est"):
            v = s.get(k)
            row[k] = [round(float(x), 5) for x in v] if v is not None else None
        for k in ("temperature", "mode", "motorstate", "error"):
            v = s.get(k)
            row[k] = [int(x) for x in v] if v is not None else None
        for k in ("power_v", "power_a"):
            v = s.get(k)
            row[k] = round(float(v), 3) if v is not None else None
        out[side] = row
    return out


def _hand_summary(hand, backoff, thermal=None):
    """/status: the hottest motor, any error, the current, and what is backed off."""
    st = _hand_state_message(hand) or {}
    out = {}
    for side in ("left", "right"):
        s = st.get(side)
        if not isinstance(s, dict):
            out[side] = None
            continue
        temps = s.get("temperature") or []
        motorstate = s.get("motorstate") or []
        err = s.get("error") or []
        out[side] = {"temp_max_c": max(temps) if temps else None,
                     "motorstate_nonzero": [i for i, v in enumerate(motorstate) if v] or [],
                     "error": err or None,
                     "power_a": s.get("power_a"),
                     "backed_off": list((backoff or {}).get(side) or []),
                     # Joints whose gain is being held down because they are
                     # getting hot. Visible so a weak grip has a stated reason
                     # rather than looking like the fault it exists to prevent.
                     "thermal": (thermal or {}).get(side) or None}
    return out


def _arm_reference_message(cmd):
    arm = getattr(cmd, "arm", None)
    return arm.arm_reference_report() if hasattr(arm, "arm_reference_report") else None


def idle_q_message(cmd):
    with cmd.lock:
        q = cmd.measured_arm_q
        body = getattr(cmd, "measured_body", None)
        motor_temperature = getattr(cmd, "measured_motor_temperature", None)
        measured_t = getattr(cmd, "measured_arm_t", None)
    return {"state": cmd.state,
            "locomotion": (cmd.locomotion.status()
                           if getattr(cmd, "locomotion", None) else {"enabled": False}),
            "homing": getattr(cmd, "homing", False),
            "input_loss_recovery": getattr(cmd, "input_loss_recovery", None),
            "arm_reference": _arm_reference_message(cmd),
            "q": [round(float(v), 5) for v in q] if q is not None else None,
            "q_age_s": (max(0.0, time.monotonic() - measured_t)
                        if measured_t is not None else None),
            "body": _body_message(body),
            "motor_temperature": _motor_temperature_message(motor_temperature),
            "hand_state": _hand_state_message(getattr(cmd, "hand", None))}


class Commander:
    last_refusal = None        # why apply() last refused a frame; None after a success

    def locomotion_gate(self):
        with self.lock:
            ready = (self.state == "ARMED" and self.owner is not None
                     and not self.discover and not getattr(self, "homing", False)
                     and self.deadman_ok() and self.lowstate_ok()
                     and 0 <= time.time() - self.lowstate_t <= .25
                     and 0 <= time.time() - self.last_frame_t <= .25)
            return ready, self.n_armed

    def __init__(self):
        # The native recorder is passive and dormant unless the bridge opens
        # /readings. Its capture hook returns immediately with no snapshot or
        # serialization work when there is no subscriber.
        from raw_telemetry import RawTelemetry
        self.telemetry = RawTelemetry()
        self.motion_limits = {"max_velocity_rad_s": ArmReference.max_velocity,
                              "max_acceleration_rad_s2": ArmReference.max_acceleration}
        try:
            with open(MOTION_LIMITS_PATH) as f:
                self.motion_limits = validate_motion_limits(json.load(f))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            print("[onboard] invalid saved motion limits; using bounded defaults")
        self.state = "STANDBY"
        self.arm = None
        self.hand = None
        self.audio = None
        self.last_frame_t = 0.0
        self.last_apply_t = 0.0
        # One state/arm lock for the watchdog and aiohttp command threads;
        # previously nothing stopped them interleaving on the controller. Dex3 has
        # its own lock because its DDS write takes ~33 ms on this robot while
        # the arm write takes <1 ms. Holding this state/arm lock across both
        # serialized the arm behind the hand and discarded about two fifths of
        # the 60 Hz arm targets. State transitions take hand_lock first, then
        # this lock, so HOLD/relax still cannot race a hand write.
        self.lock = threading.RLock()
        self.hand_lock = threading.RLock()
        self.owner = None          # the single operator's token
        self._stop = False

        # dead-man, from the robot's own remote
        self.deadman_bit = None    # set from --deadman
        self._last_cmd = None      # last commanded arm pose, for the slew limit
        self._last_cmd_t = 0.0
        self.lowstate_t = 0.0      # last time the robot said anything at all
        # Why did it leave ARMED? The device log is not reliably retrievable,
        # and "it stopped following" has several causes that look identical
        # from outside. Count them instead of guessing.
        self.n_applied = 0         # frames that reached a motor command
        self.n_superseded = 0      # buffered frames replaced by a newer target
        self.n_rejected = 0        # frames refused by sanitize_*
        self.n_watchdog = 0        # times the liveness watchdog froze us
        self.n_armed = 0           # times arm_on succeeded
        self.last_rx_t = 0.0       # last pose frame RECEIVED, applied or not
        self.measured_arm_q = None # joints 15-28, straight from lowstate
        self.measured_arm_t = None # time.monotonic() it was taken; the leash widens with its age
        self.lowstate_hub = None   # set by main(): the process's one rt/lowstate reader
        self.measured_body = None  # BODY_IDX q/dq/tau + tick, straight from lowstate
        self.measured_motor_temperature = None  # all active motors 0..28
        # Waist yaw (motor 12). Its own slew chain, because it has its own
        # rate; None until the first waist frame of an ARMED session, and
        # re-seeded from measurement after every freeze, like the arm's.
        self._waist_last_cmd = None
        self._waist_last_t = 0.0
        self._waist_active = False     # has a waist ever been commanded this hold?
        self._waist_home = None        # measured at take_control; where go_home returns it
        self._waist_announced = False
        self.last_waist = None         # {cmd, q, clamped, leashed} for the ack
        self.last_waist_refusal = None
        self.last_waist_seq = None
        self.n_waist_received = 0
        self.n_waist_applied = 0
        self.n_waist_superseded = 0
        self.n_waist_refused = 0
        # Short rolling history of commanded and measured arm vectors, so
        # "commanded a lot, moved nothing" is answerable. ~3 s at 50 Hz.
        self._cmd_hist = collections.deque(maxlen=150)
        self._meas_hist = collections.deque(maxlen=150)
        self._hold_pose = None     # arm pose captured when control was taken
        self.cal = None            # {"lo": [...], "hi": [...]} while recording
        self.n_rx = 0              # pose frames READ off the socket
        self._rx_age = []          # recent (t_recv, now - t_sent), for the trend
        self._rx_min = None        # lowest age THIS connection
        self._rx_min_proc = None   # lowest age EVER; survives a reconnect
        self._rx_baseline_state = None   # what we were doing when the baseline began
        self.n_lowstate = 0        # lowstate callbacks seen
        self.hands_only = False    # fingers only; the arms are never commanded
        self.gains = None          # [(kp, kd)] x 14 we asked the controller for
        self.n_leashed = 0         # frames with at least one joint clamped
        self._leash_log_t = 0.0
        self._track_err = []       # recent max |command - measured|, rad
        self.arm_cmd_ms = None     # cost of one arm command
        self.hand_cmd_ms = None    # cost of one Dex3 command
        self.n_hand_received = 0   # hand targets handed to the Dex3 mailbox
        self.n_hand_applied = 0    # targets that reached ctrl_dual_hand
        self.n_hand_superseded = 0  # pending hand targets replaced by newer ones
        self.n_hand_refused = 0     # state/dead-man/error refusals in hand worker
        self.last_hand_seq = None
        self.last_hand_refusal = None
        self.home_q = _load_home()  # captured ready pose, or None
        self.homing = False
        self._home_generation = 0
        self.input_loss_recovery = None
        # A finger stalled against an object is backed off to measured + a
        # margin instead of held at kp x the full gap (see dex3_stall.py and
        # DEBUG-FINGERS.md: the knuckles fell to 12-17 % follow in one session
        # and did not recover). G1_DEX3_STALL_BACKOFF=0 disables it.
        self._stall = StallBackoff.from_env()
        self._hand_backoff_names = {"left": [], "right": []}
        self.last_freeze_why = ""
        self._busy = False         # a take_control is in flight
        self.deadman_held = False
        self.remote_raw = 0
        self.remote_t = 0.0
        self.discover = False
        self._release_seen = False   # must let go before motion can resume
        # TTS RPC is independent of motor commands and may take seconds. A
        # dedicated worker keeps it off the WebSocket receive loop; otherwise
        # pressing B could starve pose frames long enough to trip the watchdog.
        self._speech_queue = queue.Queue(maxsize=4)
        threading.Thread(target=self._speech_worker, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def speak(self, prompt):
        """Speak one allowlisted prompt through the G1's onboard audio RPC."""
        text = SPOKEN_PROMPTS.get(prompt)
        if text is None:
            return f"speech refused: unknown prompt {prompt!r}"
        audio = getattr(self, "audio", None)
        if audio is None:
            return "speech unavailable: G1 audio client did not initialize"
        try:
            code = audio.TtsMaker(text, TTS_ENGLISH_SPEAKER_ID)
        except Exception as exc:  # noqa: BLE001 -- audio must never stop teleop
            return f"speech failed: {exc}"
        if code != 0:
            return f"speech failed: TtsMaker returned {code}"
        return f"speaking: {text}"

    def queue_speech(self, prompt):
        """Validate and enqueue TTS without blocking the control socket."""
        if prompt not in SPOKEN_PROMPTS:
            return f"speech refused: unknown prompt {prompt!r}"
        try:
            self._speech_queue.put_nowait(prompt)
        except queue.Full:
            return "speech refused: queue full"
        return f"speech queued: {SPOKEN_PROMPTS[prompt]}"

    def _speech_worker(self):
        while not self._stop:
            try:
                prompt = self._speech_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            print("[onboard]", self.speak(prompt))

    # ---- dead-man ----------------------------------------------------------
    def on_lowstate(self, msg):
        """Track that the robot is alive, and read the remote.

        This is the only thing this process does with lowstate, and it never
        writes anything.
        """
        now = time.time()
        self.n_lowstate += 1
        telemetry = getattr(self, "telemetry", None)
        if telemetry is not None:
            # Queue only a pointer plus exact source timestamps here. The DDS
            # object is a fresh Read() result and is never mutated by us; a
            # helper snapshots it so the lowstate poll cannot be delayed by
            # recursive conversion of the 35-motor message.
            telemetry.capture_async("rt/lowstate", msg)
        # Decimate. This fires at ~300 Hz and took the lock twice each time, to
        # service a 1 s liveness check and a dead-man that is not even
        # configured -- competing for the GIL with the socket loop that has to
        # read 97 hand frames a second.
        # Process the FIRST message and then every Nth. Skipping the first
        # would mean the robot has to talk for 10 messages before we admit it
        # is alive, which take_control checks before grabbing the arms.
        if (self.n_lowstate - 1) % LOWSTATE_DECIMATE:
            return
        # The measured arm joints, so home can be captured from a robot nobody
        # is commanding: posing the arms by hand is impossible while we hold
        # them, and reading the controller's idea of the pose would require
        # grabbing them first.
        try:
            arm_q = [float(msg.motor_state[i].q) for i in range(15, 29)]
        except (AttributeError, IndexError, TypeError, ValueError):
            arm_q = None
        now_mono = time.monotonic()
        body = _body_slice(msg, now_mono=now_mono) if arm_q is not None else None
        motor_temperature = _motor_temperature_slice(msg, now_mono=now_mono)
        raw = getattr(msg, "wireless_remote", None)
        with self.lock:
            if arm_q is not None:
                self.measured_arm_q = arm_q
                # When it was taken, for the leash (see MAX_TRACKING_ERROR_RAD).
                self.measured_arm_t = now_mono
                if body is not None:
                    self.measured_body = body
                if motor_temperature is not None:
                    self.measured_motor_temperature = motor_temperature
                if self.cal is not None:
                    for i, v in enumerate(arm_q):
                        if v < self.cal["lo"][i]:
                            self.cal["lo"][i] = v
                        if v > self.cal["hi"][i]:
                            self.cal["hi"][i] = v
            # Liveness first, and unconditionally. A lowstate carrying no remote
            # payload still proves the robot is publishing; folding the two
            # together would make a missing remote look like a dead robot.
            self.lowstate_t = now
        if raw is None or len(raw) < 4:
            return
        value = (int(raw[3]) << 8) | int(raw[2])
        with self.lock:
            if self.discover and value != self.remote_raw:
                bits = [n for n, b in REMOTE_BITS.items() if value & (1 << b)]
                print(f"[onboard] remote 0x{value:04x}  bits held: {bits or '(none)'}")
            self.remote_raw = value
            self.remote_t = time.time()
            held = bool(self.deadman_bit is not None and value & (1 << self.deadman_bit))
            if not held:
                self._release_seen = True      # a genuine release re-enables arming
            self.deadman_held = held and self._release_seen

    def deadman_ok(self) -> bool:
        """True only while the operator is actively holding the button.

        Stale lowstate counts as released: if we cannot see the remote we do not
        get to assume it is held.
        """
        with self.lock:
            if self.deadman_bit is None:
                return True                    # not configured; see the startup warning
            if time.time() - self.remote_t > REMOTE_STALE_S:
                return False
            return self.deadman_held

    def note_rx_age(self, age: float) -> None:
        with self.lock:
            self._rx_age.append((time.time(), age))
            if len(self._rx_age) > 400:
                del self._rx_age[:-400]
            # Kept OUTSIDE the rolling window on purpose. Taking the minimum
            # over the window has the very blind spot it is meant to close: in
            # a saturated steady state every sample is equally late, so the
            # window minimum IS the saturated age and the excess collapses to
            # zero. At ~4 frames/s a 400-sample window rolls past the
            # pre-saturation transient in 100 s and the signal disappears
            # exactly when the operator has been driving long enough to care.
            # The connection begins before the queue builds, so a per-connection
            # minimum captures the true clock offset once and holds it.
            if self._rx_min is None or age < self._rx_min:
                self._rx_min = age
            # Process-lifetime minimum as well. What the minimum estimates --
            # the clock offset between two hosts -- does NOT change on
            # reconnect; only the queue does. So an older connection's minimum
            # is still a valid baseline, and it rescues the case the
            # per-connection one gets wrong: a sink that reconnects while
            # ARMED (it retries every 2 s, and the wifi has been the least
            # reliable link here) takes its first reads already under load, so
            # its minimum is biased high and excess_s understates.
            #
            # The one thing a process-lifetime minimum cannot survive is a
            # clock STEP on either host mid-session, which would read as sudden
            # saturation. That is rare and loud; the reconnect bias is
            # plausible and silent. Keep both, use the smaller.
            if self._rx_min_proc is None or age < self._rx_min_proc:
                self._rx_min_proc = age

    def reset_rx_age(self) -> None:
        """Called when an operator connects.

        The minimum below is only meaningful within one socket: a reconnect
        gives a fresh queue, and carrying the old minimum across would
        understate the clock offset and read as excess that is not there.
        """
        with self.lock:
            self._rx_age = []
            self._rx_min = None
            # A baseline taken while already ARMED or HOLDING is taken under
            # the very load it is meant to measure. Record it so the status
            # block can say so, rather than leaving the reader to remember.
            self._rx_baseline_state = self.state

    def rx_age_trend(self):
        """Is there a backlog, and is it growing?

        Drift alone is not enough. A backlog does NOT grow forever: once the
        socket buffers fill, the sender blocks at its high-water mark and the
        queue settles at a CONSTANT depth. Every frame is then read several
        seconds late, drift is ~0, and a drift-only verdict says "no backlog,
        the reader is slow" -- recommending a process split when the fix is a
        latest-wins drain. At ~100 frames/s arriving and ~4/s read, saturation
        arrives within tens of seconds of arming, so that steady state is the
        regime an operator is most likely to be looking at.

        The minimum over the window is the clock offset plus best-case transit,
        whatever the offset happens to be. Everything above it is queue, and a
        constant skew shifts the minimum and the latest equally.
        """
        with self.lock:
            w = list(self._rx_age)
        if len(w) < 20:
            return None
        ages = [a for _, a in w]
        h = len(ages) // 2
        drift = sum(ages[h:]) / (len(ages) - h) - sum(ages[:h]) / h
        with self.lock:
            mins = [m for m in (self._rx_min, self._rx_min_proc) if m is not None]
            baseline_state = self._rx_baseline_state
        lo = min(mins) if mins else min(ages)
        excess = ages[-1] - lo

        if drift > 0.25:
            verdict = "GROWING BACKLOG — frames queuing in the socket (F5: latest-wins drain)"
        elif excess > 0.5:
            verdict = ("SATURATED BACKLOG — queue at a constant depth, every frame "
                       f"read {excess:.1f}s late (F5: latest-wins drain)")
        elif excess < 0.1:
            verdict = "no backlog — the read loop itself is slow (D1: split the process)"
        else:
            verdict = f"inconclusive — excess {excess:.2f}s is between the thresholds"
        return {
            "samples": len(w),
            "window_s": round(w[-1][0] - w[0][0], 1),
            "latest_s": round(ages[-1], 3),
            "min_s": round(lo, 3),          # clock offset + best-case transit
            "min_connection_s": (round(self._rx_min, 3)
                                 if self._rx_min is not None else None),
            "min_process_s": (round(self._rx_min_proc, 3)
                              if self._rx_min_proc is not None else None),
            "baseline_caveat": (
                f"baseline taken in {baseline_state} — may understate excess"
                if baseline_state not in (None, "STANDBY") else ""),
            "excess_s": round(excess, 3),
            "drift_s": round(drift, 3),
            "verdict": verdict,
            "note": ("latest_s is only meaningful if both hosts share an NTP "
                     "source; excess_s and drift_s do not care"),
        }

    def following_report(self):
        """Is the arm moving at all, or only being talked to?

        `lowstate_ok()` cannot answer this: lowstate publishes in damping mode
        too, so a robot that is powered on but NOT in Main Operation Control
        looks healthy, accepts take_control, accepts arm, reports frames
        applied -- and does not move, because rt/arm_sdk blends into a motion
        controller that is not running. The Dex3 hands keep working throughout,
        being on their own topic, so the operator sees their fingers move and
        reasonably concludes the arms are broken.

        That cost a long detour today, and the operator spotted it, not the
        diagnostics. So: compare how far each joint was TOLD to move against how
        far it actually moved, over a window. Large commands with no motion is a
        specific, nameable condition rather than a mystery.
        """
        with self.lock:
            hist = list(getattr(self, "_cmd_hist", ()))
            meas = list(getattr(self, "_meas_hist", ()))
        if len(hist) < 30 or len(meas) < 30:
            return {"samples": len(hist)}
        c = np.asarray(hist)
        m = np.asarray(meas)
        commanded = float(np.max(c.max(axis=0) - c.min(axis=0)))
        moved = float(np.max(m.max(axis=0) - m.min(axis=0)))
        out = {"samples": len(hist),
               "commanded_deg": round(math.degrees(commanded), 1),
               "moved_deg": round(math.degrees(moved), 1)}
        if commanded > math.radians(15) and moved < 0.1 * commanded:
            out["verdict"] = (
                "NOT MOVING: commanded "
                f"{math.degrees(commanded):.0f} deg, moved "
                f"{math.degrees(moved):.0f}. The arms are being commanded and "
                "are not responding. Most likely the robot is not in Main "
                "Operation Control (running mode) -- rt/arm_sdk blends into "
                "the motion controller, and with it down there is nothing to "
                "blend into. The Dex3 hands are on a separate topic and will "
                "keep working, which makes this look like an arm fault.")
        return out

    def tracking_report(self):
        """Command minus measured -- verification-plan measurement #2, which the
        leash hands us as a side effect."""
        with self.lock:
            e = sorted(self._track_err)
            n = self.n_leashed
            cur = self._track_err[-1] if self._track_err else None
        if not e:
            return {"samples": 0, "clamped_frames": n}
        p95 = e[min(len(e) - 1, int(len(e) * 0.95))]
        return {
            "samples": len(e),
            "current_deg": round(math.degrees(cur), 2),
            "p95_deg": round(math.degrees(p95), 2),
            "max_deg": round(math.degrees(e[-1]), 2),
            "limit_deg": round(math.degrees(MAX_TRACKING_ERROR_RAD), 1),
            "clamped_frames": n,
            "note": ("a joint clamped persistently is stuck, overloaded, or being "
                     "commanded somewhere it cannot go" if n else ""),
        }

    def lowstate_ok(self) -> bool:
        """True if the robot is currently publishing lowstate."""
        with self.lock:
            return bool(self.lowstate_t) and \
                time.time() - self.lowstate_t <= LOWSTATE_STALE_S

    def fault(self, why: str) -> None:
        """Require a deliberate release-and-retake before motion can resume."""
        with self.lock:
            self._release_seen = False
            self.deadman_held = False
        print(f"[onboard] fault: {why} — release the dead-man and take hold again")

    # ---- single-operator ownership ----------------------------------------
    # How long an owner may be silent before a new connection may take over.
    # Generous next to the 1 s liveness watchdog: this is not about tracking
    # dropouts, it is about a socket that is gone and has not said so.
    OWNER_STALE_S = 10.0

    def claim(self):
        """Hand out control to exactly one connection.

        Without this, any second client that connected and dropped ran the
        disconnect cleanup and released the arms out from under the real
        operator -- a health check or a stray browser tab was enough.

        But a claim with no expiry is a lockout waiting to happen, and it
        happened: redeploying the bridge left its socket HALF-OPEN, so the
        owning connection's disconnect handler never ran, the claim was never
        released, and every new bridge was refused with "another operator holds
        control" -- with no way back except restarting the robot's app.

        So an owner that has gone silent for OWNER_STALE_S can be displaced. A
        live operator sends frames continuously and cannot be taken over; a
        dead socket sends nothing and stops blocking the robot. The arms are
        FROZEN on takeover rather than left following the newcomer, because a
        change of operator is not a moment to keep moving.
        """
        with self.lock:
            if self.owner is not None:
                age = time.time() - (self.last_rx_t or 0.0)
                if age < self.OWNER_STALE_S:
                    return None
                print(f"[onboard] taking control from an owner silent for "
                      f"{age:.0f}s — freezing the arms")
                self.owner = None
                self._freeze()
                # HOLDING, not ARMED: the new operator must arm deliberately.
                # Inheriting an armed robot from a connection that died is how
                # someone ends up moving arms they did not know were live.
                if self.state == "ARMED":
                    self.state = "HOLDING"
            self.owner = secrets.token_hex(8)
            self.last_rx_t = time.time()
            return self.owner

    def release(self, token):
        """Give up control. Ignored from anyone who does not hold it."""
        with self.lock:
            if token != self.owner:
                return False
            self.owner = None
            return True

    def owns(self, token):
        with self.lock:
            return token is not None and token == self.owner

    # ---- state transitions -------------------------------------------------
    def take_control(self):
        if self.discover:
            # Discovery exists to identify remote buttons on the real robot
            # before a dead-man is configured. Letting it grab the arms would
            # mean a window on hardware with no dead-man at all, so it cannot.
            return "refused: discovery mode is read-only; restart without --deadman-discover"
        with self.lock:
            if self.arm is not None:
                return "already holding"
            if self._busy:
                return "refused: a take_control is already in flight"
            # Check BEFORE constructing the controller. Its constructor blocks
            # waiting for lowstate, so without this the failure mode for "robot
            # not in operation mode" was a hang rather than an answer.
            if not self.lowstate_ok():
                return ("refused: no lowstate from the robot. Is it powered and in "
                        "Main Operation Control? We publish on rt/arm_sdk, which "
                        "blends into the motion controller -- with that controller "
                        "down there is nothing to blend into.")
            self._busy = True
        try:
            from g1_arm import G1_29_ArmController
            from g1_dex3 import Dex3Sender
            telemetry = getattr(self, "telemetry", None)
            capture = telemetry.capture if telemetry is not None else None
            # The process's one lowstate reader (lowstate_hub.py), not a second
            # one of its own. getattr: tests build Commander without __init__.
            arm = G1_29_ArmController(motion_mode=True,
                                      record_reading=capture,
                                      record_lowcmd=(telemetry.capture_lowcmd
                                                     if telemetry is not None else None),
                                      lowstate_hub=getattr(self, "lowstate_hub", None))
            # Hold the MEASURED pose immediately. The vendored controller seeds
            # q_target = zeros in its constructor and its publish thread starts
            # right there, so without this, taking control commands both arms to
            # the zero pose at up to 20 rad/s -- an uncommanded move, while the
            # state machine reports "holding current pose". Observed on the wire:
            # arm_q all zeros at weight 1.0 immediately after take_control.
            arm.ctrl_dual_arm(arm.get_current_dual_arm_q(), np.zeros(ARM_DIM))
            hand = Dex3Sender(kp=DEX3_KP, kd=DEX3_KD,
                              record_reading=capture)
        except Exception as e:  # noqa: BLE001 — a failed grab must not kill the server
            print("[onboard] take_control failed:", e)
            return f"refused: could not grab the arms: {e}"
        finally:
            with self.lock:
                self._busy = False
        with self.lock:
            self.arm, self.hand = arm, hand
            # The baseline a partial frame is laid over.
            self._hold_pose = list(np.asarray(arm.get_current_dual_arm_q(), float))
            # Where go_home returns the waist: where it was when we took the
            # arms. None when the onboard has no body state yet, and then
            # go_home leaves the waist alone rather than guessing zero.
            self._waist_home = self.measured_waist_q()
            self._waist_active = False
            self.state = "HOLDING"
            self._init_gains(arm)
        return "HOLDING — arms grabbed, holding current pose"

    def take_hands(self):
        """Take the HANDS only. The arms are never touched.

        Deliberately does not construct G1_29_ArmController, and that is the
        point three times over:

        - Safety: without it nothing is ever published to rt/arm_sdk, so no
          arm motion is physically possible from this process.
        - Throughput: the arm controller is the process's two biggest
          interpreter costs -- a 50 Hz publish loop doing a Python CRC over 35
          motors, and a SECOND lowstate subscription deserialising a 35-motor
          struct at ~280 Hz. Without it the socket loop is not starved.
        - Correctness: DexPilot runs on WRIST-LOCAL landmarks, which are
          position- and orientation-invariant, so none of the frame, offset or
          scale machinery that took two days to get right is involved here.
          There is nothing left in this path to aim wrongly.
        """
        if self.discover:
            return "refused: discovery mode is read-only"
        with self.lock:
            if self.arm is not None:
                return "refused: arms are held; relax first"
            if self.hand is not None:
                return "already holding the hands"
            if not self.lowstate_ok():
                return "refused: no lowstate — is the robot powered and talking?"
            self._busy = True
        try:
            from g1_dex3 import Dex3Sender
            telemetry = getattr(self, "telemetry", None)
            capture = telemetry.capture if telemetry is not None else None
            hand = Dex3Sender(kp=DEX3_KP, kd=DEX3_KD,
                              record_reading=capture)
        except Exception as e:  # noqa: BLE001
            print("[onboard] take_hands failed:", e)
            return f"refused: could not open the hands: {e}"
        finally:
            with self.lock:
                self._busy = False
        with self.lock:
            self.hand = hand
            self.hands_only = True
            self.state = "HOLDING"
        return ("HOLDING (HANDS ONLY) — the arms are not held and cannot move "
                "from this process. arm to start following your fingers.")

    def arm_on(self):
        with self.lock:
            if (getattr(self, "homing", False) or
                    (getattr(self, "input_loss_recovery", None) or {}).get("phase") == "returning"):
                return "refused: homing is still in progress"
            if self.arm is None and not self.hands_only:
                return "refused: take_control or take_hands first"
            # Refuse to arm into an instant freeze. Arming with no hand data
            # sets ARMED, the watchdog fires 0.3 s later, and the operator is
            # left looking at a robot that reports ARMED-then-HOLDING and never
            # moved -- indistinguishable from a broken link. Observed on
            # hardware: two frames applied, then frozen for 40 s.
            age = time.time() - self.last_rx_t if self.last_rx_t else None
            if age is None or age > RX_REQUIRED_S:
                return ("refused: no hand data arriving"
                        + (f" ({age:.1f}s since the last frame)" if age else "")
                        + ". Both hands must be tracked BEFORE arming — put the "
                          "headset on and check the control page reads 2 hands.")
            # No speed ramp. The controller's old speed_gradual_max() went
            # 20 -> 30 rad/s over 5 s and never protected the arming snap. The
            # motor-clock reference now owns the live velocity/acceleration
            # bounds, with the measured-position leash and calibrated limits
            # still taking precedence. The controller retains one fixed final
            # clip (Unitree removed the ramp upstream too, xr_teleoperate
            # 4cbf6e2).
            # Re-seed from the measured pose so the first frame slews from where
            # the arms ACTUALLY are, not from a stale target left by a previous
            # session.
            self._last_cmd = None
            self.n_armed += 1
            self.last_freeze_why = ""
            self.state = "ARMED"
            self.last_frame_t = time.time()
        return "ARMED — teleop motion enabled"

    def limp_arms(self, kd: float = 1.5):
        """Soften the arms so a person can pose them, legs untouched.

        Grabs arm_sdk if we do not already hold it. The legs stay with the
        robot's balance controller -- this is deliberately NOT the remote's
        damping combo, which limps everything and drops the robot.
        """
        if self.state == "ARMED":
            return "refused: hold first; going limp while armed is a surprise"
        if self.arm is None:
            reply = self.take_control()
            if not reply.startswith("HOLDING"):
                return reply
        with self.lock:
            self.arm.set_arm_compliance(kp=0.0, kd=kd)
            self.state = "LIMP"
            self._last_cmd = None
        return ("LIMP — arms are back-drivable, legs unaffected. They have no "
                "gravity compensation, so they will sag. `hold` to stiffen "
                "them again at wherever they end up.")

    def calibrate_start(self):
        """Begin recording the range each arm joint is walked through.

        Like lerobot's calibrate: move the joints by hand through the range you
        are willing to use, then stop. Takes no control -- reading lowstate is
        enough, and holding the arms would defeat the point.
        """
        with self.lock:
            if self.state not in ("STANDBY", "LIMP"):
                return ("refused: calibration is done by hand. Use `limp` to "
                        "soften the arms, or release first.")
            if self.measured_arm_q is None:
                return "refused: no lowstate — is the robot powered?"
            q = list(self.measured_arm_q)
            # Seed from the previous pass so this one EXTENDS rather than
            # replaces. Wiping meant fixing two missed joints cost a full
            # re-walk of all fourteen.
            prev = _load_cal_observed()
            if prev:
                self.cal = {"lo": [min(a, b) for a, b in zip(prev["lo"], q)],
                            "hi": [max(a, b) for a, b in zip(prev["hi"], q)]}
                topping = True
            else:
                self.cal = {"lo": list(q), "hi": list(q)}
                topping = False
        return (("topping up the existing calibration — only the joints you "
                 "move now will change; the rest keep what they had. ")
                if topping else
                ("calibrating from scratch — move EVERY arm joint through its "
                 "safe range by hand. ")) + (
                "calibrate_stop when done. Watch for self-collision; the "
                "mechanical limits let the arms reach the torso.")

    def calibrate_stop(self):
        """Turn the recorded extremes into limits, and say what was skipped."""
        with self.lock:
            cal = self.cal
            self.cal = None
        if cal is None:
            return "refused: not calibrating"

        limits, skipped = [], []
        for i, (lo, hi) in enumerate(zip(cal["lo"], cal["hi"])):
            mlo, mhi = ARM_LIMITS[i]
            if hi - lo < CAL_MIN_TRAVEL:
                # Never lock a joint to a range nobody exercised. Clamping the
                # wrists to 2 degrees because the operator only moved the
                # shoulders would freeze teleop and look like a broken link.
                limits.append((mlo, mhi))
                skipped.append(i)
                continue
            # Pull IN from the observed extremes, and never wider than the
            # mechanical limit: a calibration may only make the envelope
            # smaller. Intersecting is what makes this safe to run casually.
            limits.append((max(mlo, lo + CAL_MARGIN_RAD),
                           min(mhi, hi - CAL_MARGIN_RAD)))
        try:
            os.makedirs(os.path.dirname(CAL_PATH) or ".", exist_ok=True)
            with open(CAL_PATH, "w") as fh:
                json.dump({"limits": [list(x) for x in limits],
                           "observed": {"lo": cal["lo"], "hi": cal["hi"]}}, fh)
        except OSError as e:
            return f"refused: could not save calibration: {e}"

        global ARM_LIMITS_CAL
        ARM_LIMITS_CAL = limits
        msg = f"calibrated {ARM_DIM - len(skipped)}/{ARM_DIM} joints"
        if skipped:
            msg += (f"; joints {skipped} were not exercised and keep their "
                    "mechanical limits")
        return msg

    def calibrate_cancel(self):
        """Abandon a recording in progress, keeping the saved calibration.

        There was no way to do this. `calibrate_stop` APPLIES what was walked
        -- so a recording started and then thought better of would, on stop,
        reset every unexercised joint to its mechanical limit. `calibrate_clear`
        deletes the saved calibration outright. Neither is "never mind".

        It matters because calibrate_start announces whether it is topping up
        or starting from scratch, and the honest response to "from scratch,
        and I only wanted to fix one joint" is to back out.
        """
        with self.lock:
            had = self.cal is not None
            self.cal = None
        return ("recording abandoned; the saved calibration is untouched"
                if had else "not calibrating")

    def calibrate_clear(self):
        global ARM_LIMITS_CAL
        with self.lock:
            self.cal = None
        ARM_LIMITS_CAL = None
        try:
            os.remove(CAL_PATH)
        except OSError:
            pass
        return "calibration cleared; back to mechanical limits"

    def set_home(self, delay_s: float = 0.0):
        """Record the arms' current pose as home.

        Capturing beats hard-coding: the G1 arm chain's sign conventions are
        easy to get backwards, and a home pose written from a guess is a
        confident unattended move to the wrong place.
        """
        if delay_s:
            print(f"[onboard] capturing home in {delay_s:.0f}s — pose the arms now")
            time.sleep(delay_s)
        with self.lock:
            # Deliberately does NOT require take_control. Home is most naturally
            # set by posing the arms by hand, which cannot be done while we are
            # holding them against you.
            if self.measured_arm_q is None:
                return "refused: no lowstate — is the robot powered and talking?"
            q = [round(float(v), 5) for v in self.measured_arm_q]
        try:
            os.makedirs(os.path.dirname(HOME_PATH) or ".", exist_ok=True)
            with open(HOME_PATH, "w") as fh:
                json.dump({"arm": q}, fh)
        except OSError as e:
            return f"refused: could not save home: {e}"
        with self.lock:
            self.home_q = q
        return f"home captured: {[round(v, 2) for v in q]}"

    def _hand_backoff(self, hand, left7, right7):
        """Back stalled finger joints off to measured + margin. Never raises.

        Uses the hand's own 10 Hz measured sample (measured() returns the
        last one between reads). A hand that does not report leaves the
        command untouched: nothing here guesses a position.
        """
        names = {"left": [], "right": []}
        try:
            meas = hand.measured() if hasattr(hand, "measured") else None
            if isinstance(meas, dict):
                # `mode` too: a motor the hand reports as DISABLED is faulted,
                # not stalled, and holding it back says the wrong word about it.
                st = (hand.measured_state() if hasattr(hand, "measured_state") else {}) or {}
                lm = ((st.get("left") or {}).get("mode"))
                rm = ((st.get("right") or {}).get("mode"))
                left7, names["left"] = self._stall.apply("left", left7, meas.get("left"), lm)
                right7, names["right"] = self._stall.apply("right", right7, meas.get("right"), rm)
        except Exception as e:  # noqa: BLE001 -- a guard must not cost the frame
            print("[onboard] hand back-off error:", e)
        self._hand_backoff_names = names
        return left7, right7

    def recover_input_loss(self, reason, *, _owner_token=None):
        """Leave teleop, slowly home onboard, and require deliberate re-arming.

        Only an active teleop run triggers recovery. A manual Hold, a remote
        stop, a completed return, or a second disconnect cannot start another
        move. This runs without a live bridge or headset connection.
        """
        with self.lock:
            if _owner_token is not None and not self.release(_owner_token):
                return "not the controlling connection"
            if self.state != "ARMED":
                return "no active teleop to recover"
            self._freeze()
            self.state = "HOLDING"
            self.last_freeze_why = reason
            generation = getattr(self, "_home_generation", 0) + 1
            self._home_generation = generation
            recovery = {"reason": reason, "phase": "returning", "result": None}
            self.input_loss_recovery = recovery
        try:
            result = self.go_home(_recovery_generation=generation)
        except Exception as exc:  # keep the watchdog alive after a failed recovery
            with self.lock:
                if generation == self._home_generation:
                    self._freeze()
            result = f"home recovery failed: {exc}"
        recovery.update(phase="complete" if result.startswith("at home (worst joint") else "incomplete",
                        result=result)
        print(f"[onboard] input loss: {reason} -> {result}")
        return result

    def operator_disconnected(self, token):
        return self.recover_input_loss("operator disconnected", _owner_token=token)

    def go_home(self, hands=None, *, _recovery_generation=None):
        """Slew to the captured home pose, slowly, and stop there.

        Runs in HOLDING only. Arming during a homing move would mean two things
        commanding the arms at once, so it is refused while this runs.

        `hands` is the closed pose the BRIDGE would command at a full squeeze
        ({"left": [7], "right": [7]}, bridge order), sent with the command.
        The hands close to it; without a valid one (an older bridge) they
        close to this file's own copy, which has thumb_0 neutral -- the pose
        the operator reported as "not fully closed".
        """
        with self.lock:
            if (_recovery_generation is not None and
                    _recovery_generation != getattr(self, "_home_generation", 0)):
                return "homing cancelled by a newer operator command"
            if self.arm is None:
                return "refused: take_control first"
            if self.state == "ARMED":
                return "refused: hold first; homing while armed is two drivers"
            if self.home_q is None:
                return ("refused: no home captured. Pose the arms by hand, then "
                        "set_home.")
            if self.homing:
                return "refused: already homing"
            if _recovery_generation is not None:
                if not self.lowstate_ok():
                    return "refused: no fresh joint feedback for automatic home"
                if not self.deadman_ok():
                    return "refused: remote stop takes priority over automatic home"
            target = np.asarray(self.home_q, float)
            if target.shape != (ARM_DIM,) or not np.all(np.isfinite(target)):
                return "refused: saved home pose is invalid"
            generation = (_recovery_generation if _recovery_generation is not None
                          else getattr(self, "_home_generation", 0) + 1)
            self._home_generation = generation
            self.homing = True
            arm = self.arm
            hand = self.hand
            # The waist comes home too, but only if teleop ever drove it in
            # this hold: an arm-only session must leave motor 12 exactly as
            # it found it, and a controller that never received a waist
            # target must never start receiving one from homing.
            waist_home = getattr(self, "_waist_home", None)
            waist_from = (self._waist_last_cmd if getattr(self, "_waist_active", False)
                          and self._waist_last_cmd is not None else None)
            if waist_from is None and getattr(self, "_waist_active", False):
                waist_from = self.measured_waist_q()
            if waist_home is None or not getattr(self, "_waist_active", False):
                waist_from = None
        pose = sanitize_hand_pose(hands)
        if pose is not None:
            closed_l, closed_r = pose
            hands_src = "bridge grip"
        else:
            closed_l, closed_r = list(CLOSED_LEFT), list(CLOSED_RIGHT)
            hands_src = "onboard default" + (", payload refused" if hands is not None else "")
        q = None
        try:
            q = np.asarray(arm.get_current_dual_arm_q(), float)
            dt = 0.02
            while True:
                step = HOME_SLEW_RAD_S * dt
                # Serialize the cancellation check with the write. A Hold or
                # Release must never be followed by one more homing command.
                with self.lock:
                    if (not self.homing or self.state != "HOLDING" or
                            generation != self._home_generation):
                        return "homing cancelled"
                    if _recovery_generation is not None and (
                            not self.lowstate_ok() or not self.deadman_ok()):
                        self._freeze()
                        return "homing cancelled: joint feedback lost or remote stop"
                    q = np.clip(target, q - step, q + step)
                    if waist_from is None:
                        arm.ctrl_dual_arm(q, np.zeros(ARM_DIM))
                    else:
                        waist_from = min(waist_from + step, max(waist_from - step, waist_home))
                        arm.ctrl_dual_arm(q, np.zeros(ARM_DIM), waist_yaw=waist_from)
                # The HANDS come home too. Homing moved the 14 arm joints and
                # left the Dex3 exactly as the last attempt ended it -- which
                # is closed, because the last thing an attempt does is grip.
                # "Go home" that leaves the robot holding a fist is not a
                # known starting pose, and comparing attempts is the whole
                # reason for having one.
                #
                # Open is zero on every Dex3 joint: each range straddles it,
                # and grip_q's own `opened` is zero clamped into range. Sent
                # every iteration alongside the arm so the hand opens with the
                # arm rather than snapping at one end or the other.
                if hand is not None:
                    try:
                        # CLOSED, at the operator's request. Home was opening
                        # them on the reasoning that a fist is not a known
                        # starting pose -- but a closed hand is equally known,
                        # it is what the operator asked for, and it is the
                        # compact shape to move an arm across a table in.
                        # ... to the pose the bridge's grip would command at a
                        # full squeeze when it sent one, else this file's copy.
                        hand_lock = getattr(self, "hand_lock", self.lock)
                        with hand_lock:
                            with self.lock:
                                if (not self.homing or self.state != "HOLDING" or
                                        generation != self._home_generation):
                                    return "homing cancelled"
                                hand.ctrl_dual_hand(closed_l, closed_r)
                    except Exception as e:  # noqa: BLE001 -- never strand the arm
                        print("[onboard] hand home error:", e)
                if float(np.max(np.abs(target - q))) < 1e-3 and (
                        waist_from is None or abs(waist_from - waist_home) < 1e-3):
                    break
                time.sleep(dt)
        finally:
            with self.lock:
                if generation == self._home_generation:
                    self.homing = False
                    self._last_cmd = None        # re-seed the teleop slew
                # Home is now the pose partial frames are laid over. Without
                # this, take_control -> home -> arm in forearm mode commanded
                # the unnamed shoulders back to the take_control pose. Use the
                # last pose actually commanded, not the target: a cancelled
                # homing move stopped somewhere in between.
                if q is not None and generation == self._home_generation:
                    self._hold_pose = [float(v) for v in q]
        # Did it ARRIVE? The loop above stops when the COMMAND reaches the
        # target, which says only that we finished talking. A joint held off
        # home by gravity, a table, or a gain too low to lift it reports "at
        # home" while sitting somewhere else -- and then every attempt after
        # it starts from a pose nobody chose, which is exactly what a home
        # pose exists to prevent.
        with self.lock:
            meas = self.measured_arm_q
        tail = f"; hands closed to the {hands_src} pose"
        if meas is None:
            return "at home (commanded; no measured joints to confirm it)" + tail
        err = np.abs(np.asarray(meas, float) - target)
        worst = float(err.max())
        if worst <= HOME_ARRIVED_RAD:
            return f"at home (worst joint {math.degrees(worst):.1f} deg off)" + tail
        off = ", ".join(f"{ARM_JOINT_NAMES[i]} {math.degrees(err[i]):.0f}"
                        for i in np.argsort(-err)[:3] if err[i] > HOME_ARRIVED_RAD)
        return (f"commanded home but NOT there: worst {math.degrees(worst):.1f} deg "
                f"({off}). Something is holding the arm, or the gains cannot lift it." + tail)

    def hold(self):
        """Freeze arms AND hands.

        Previously this froze only the arms. An operator calling hold because
        something is wrong stopped the arms and left the Dex3 squeezing whatever
        it had hold of, including a person.
        """
        hand_lock = getattr(self, "hand_lock", self.lock)
        # Wait for an in-flight Dex3 write, then change state and relax while
        # still owning the hand. A queued worker can run afterwards, but its
        # ARMED check will refuse the stale target before touching hardware.
        with hand_lock:
            with self.lock:
                if self.arm is None and not self.hands_only:
                    # Nothing is held, so there is nothing to hold. Reporting
                    # HOLDING here would tell an operator the arms are grabbed when
                    # they are not -- a safety state machine that lies about its
                    # state is worse than one with fewer states.
                    return "refused: nothing is held; take_control first"
                self.homing = False                  # cancel any homing move
                self._home_generation = getattr(self, "_home_generation", 0) + 1
                if self.arm is not None:
                    # Restore stiffness first: freezing at the measured pose is
                    # meaningless while the joints have no position gain to hold it.
                    # The commander's own table, so a per-joint stiffness set from
                    # the jog panel survives a limp/hold cycle instead of silently
                    # reverting to the controller's constructor values.
                    self._restore_gains()
                    self._freeze()
                if self.hand is not None:
                    try:
                        self.hand.relax()
                    except Exception as e:  # noqa: BLE001
                        print("[onboard] hand relax on hold error:", e)
                self.state = "HOLDING"
        return "HOLDING — arms frozen, hands relaxed"

    def relax(self, go_home: bool = False):
        """Release the arms.

        `go_home` defaults to FALSE, and that is the important part. This used
        to always sweep both arms to a home pose before releasing -- including
        on an unexpected disconnect, which is the least supervised moment there
        is. If the link dropped because the operator fell over or a cable was
        pulled, the robot answered by moving two arms with nobody driving.

        Freeze where it is, then hand control back. An operator who wants a
        homing motion asks for it explicitly.
        """
        hand_lock = getattr(self, "hand_lock", self.lock)
        with hand_lock:
            with self.lock:
                self.homing = False
                self._home_generation = getattr(self, "_home_generation", 0) + 1
                if self.hand is not None:
                    try:
                        self.hand.relax()
                    except Exception as e:  # noqa: BLE001
                        print("[onboard] hand relax error:", e)
                if self.arm is not None:
                    try:
                        self._freeze()                      # stop where you are
                        if go_home:
                            self.arm.ctrl_dual_arm_go_home()
                        # Stop the publisher. Without this the controller's daemon
                        # thread keeps asserting arm_sdk at weight 1.0 after we have
                        # reported STANDBY, so the robot's own controller fights a
                        # dead operator's last pose.
                        self.arm.shutdown()
                    except Exception as e:  # noqa: BLE001
                        print("[onboard] release error:", e)
                self.arm = None
                self.hand = None
                self.hands_only = False
                self.state = "STANDBY"
        return ("STANDBY — homed and released" if go_home
                else "STANDBY — frozen in place and released")

    # ---- motion ------------------------------------------------------------
    def _target_admission_refusal(self, admission):
        """Caller holds self.lock; validate queued work again at execution."""
        if admission is None:  # synchronous callers retain their existing guards
            return None
        epoch, received = admission
        if epoch != getattr(self, "_target_epoch", 0):
            return "control_transition"
        if not 0 <= time.monotonic() - received <= TARGET_MAX_AGE_S:
            return "stale_target"
        return None

    def apply_arm(self, arm14, *, waist=None, waist_clamped=False, admission=None):
        """Command the arm/waist half of one frame without waiting for Dex3.

        Returns True if a command actually went to the controller, so the
        frame's ack can say "applied" rather than merely "read".

        `waist` is an optional yaw target for motor 12 (already sanitized).
        None -- the default, and every frame from a bridge that has no waist
        dial -- leaves motor 12 exactly as it is: the controller is called
        without the argument, so nothing about an arm-only frame changes.
        `waist_clamped` is whether sanitize_waist clamped THIS frame's value,
        captured by the caller right after sanitizing -- the same discipline
        as the arm's clamped_idx, so an ack never inherits another frame's.
        """
        now = time.time()
        # Why a frame was refused, for the ack. Two can sessions had the onboard
        # apply only 37 % and 41 % of the armed frames it read, and the files
        # could not say which of these branches did it.
        self.last_refusal = None
        if not self.deadman_ok():
            self.last_refusal = "deadman"
            return False                             # nobody is holding the remote
        with self.lock:
            refusal = self._target_admission_refusal(admission)
            if refusal:
                self.last_refusal = refusal
                return False
            if self.state != "ARMED":
                self.last_refusal = "not_armed"
                return False
            if getattr(self, "homing", False):
                # Explicit, though the check above already covers it: go_home
                # only runs from HOLDING. Saying it out loud means a future
                # change that lets homing run in another state cannot quietly
                # put the operator's hands and the homing slew on the same
                # joints -- two drivers, which is the exact thing go_home
                # refuses to be half of.
                self.last_refusal = "homing"
                return False
            if self.arm is None and not self.hands_only:
                self.last_refusal = "no_arm"
                return False
            if now - self.last_apply_t < 1.0 / MAX_FRAME_HZ:
                # Direct synchronous callers still cannot flood the target
                # setter. The websocket waits in its replaceable mailbox
                # until this gate opens, so its newest intent is retained.
                self.last_refusal = "rate_limit"
                return False                             # ignore a flooding sender
            self.last_frame_t = now
            self.last_apply_t = now
            self.n_applied += 1
            arm = self.arm
            try:
                _t0 = time.time()
                if arm is not None and arm14 is not None:
                    q = self._slew(arm, np.asarray(arm14, float), now)
                    q = self._leash(q, now)
                    if waist is None:
                        arm.ctrl_dual_arm(q, np.zeros(ARM_DIM))
                    else:
                        wq, wm, wleash = self._waist_step(waist, now)
                        if not self._waist_announced:
                            self._waist_announced = True
                            lo, hi = effective_waist_limits()
                            print(f"[onboard] waist yaw enabled, cap "
                                  f"{math.degrees(lo):.0f}..{math.degrees(hi):.0f} deg, "
                                  f"slew {WAIST_SLEW_RAD_S} rad/s")
                        self._waist_active = True
                        self.last_waist = {"cmd": round(float(wq), 5),
                                           "q": (round(wm, 5) if wm is not None else None),
                                           "clamped": bool(waist_clamped),
                                           "leashed": bool(wleash)}
                        arm.ctrl_dual_arm(q, np.zeros(ARM_DIM), waist_yaw=wq)
                self.arm_cmd_ms = round((time.time() - _t0) * 1000.0, 3)
                return True
            except Exception as e:  # noqa: BLE001 — never let one bad frame kill the loop
                print("[onboard] arm command error:", e)
                self.last_refusal = f"error: {e}"[:80]
                return False

    def apply_hands(self, left7, right7, *, seq=None, admission=None):
        """Command only Dex3, serialized independently from the arm/waist.

        The dedicated websocket worker calls this at the rate the physical
        hand publisher can sustain. Its mailbox is newest-wins, so slow Dex3
        cannot queue stale finger poses or occupy the arm worker. HOLD and
        relax take ``hand_lock`` before changing state, which keeps this split
        from allowing a stale hand write after a stop.
        """
        self.last_hand_refusal = None
        hand_lock = getattr(self, "hand_lock", self.lock)
        with hand_lock:
            if not self.deadman_ok():
                self.last_hand_refusal = "deadman"
            else:
                with self.lock:
                    refusal = self._target_admission_refusal(admission)
                    if refusal:
                        self.last_hand_refusal = refusal
                    elif self.state != "ARMED":
                        self.last_hand_refusal = "not_armed"
                    elif getattr(self, "homing", False):
                        self.last_hand_refusal = "homing"
                    else:
                        hand = self.hand if SEND_HANDS else None
                if self.last_hand_refusal is None and hand is None:
                    # Hands are optional. No Dex3 controller means there is no
                    # hand half to apply, not that the arm frame failed.
                    return True
                if self.last_hand_refusal is not None:
                    self.n_hand_refused = getattr(self, "n_hand_refused", 0) + 1
                    return False

                try:
                    _t0 = time.time()
                    left7, right7 = self._hand_backoff(hand, left7, right7)
                    hand.ctrl_dual_hand(left7, right7)
                    self.hand_cmd_ms = round((time.time() - _t0) * 1000.0, 3)
                    self.n_hand_applied = getattr(self, "n_hand_applied", 0) + 1
                    self.last_hand_seq = seq
                    return True
                except Exception as e:  # noqa: BLE001
                    print("[onboard] hand command error:", e)
                    self.last_hand_refusal = f"error: {e}"[:80]
                    self.n_hand_refused = getattr(self, "n_hand_refused", 0) + 1
                    return False

    def apply_waist(self, target, *, waist_clamped=False, seq=None, admission=None):
        """Refresh only waist yaw; the arm publisher remains the sole DDS writer.

        The websocket owns a dedicated newest-wins waist worker, but this
        method only updates ``G1_29_ArmController.waist_yaw_target``. The
        controller's existing LowCmd loop publishes motor 12 beside the arms,
        so decoupling network delivery cannot introduce a competing publisher.
        Waist traffic deliberately does not refresh ``last_frame_t``: a stuck
        thumbstick stream must never keep the arm watchdog alive after arm
        tracking stops.
        """
        now = time.time()
        self.last_waist_refusal = None
        if not self.deadman_ok():
            self.last_waist_refusal = "deadman"
        else:
            with self.lock:
                refusal = self._target_admission_refusal(admission)
                if refusal:
                    self.last_waist_refusal = refusal
                elif self.state != "ARMED":
                    self.last_waist_refusal = "not_armed"
                elif getattr(self, "homing", False):
                    self.last_waist_refusal = "homing"
                elif self.arm is None:
                    self.last_waist_refusal = "no_arm"
                else:
                    try:
                        wq, wm, wleash = self._waist_step(target, now)
                        if not self._waist_announced:
                            self._waist_announced = True
                            lo, hi = effective_waist_limits()
                            print(f"[onboard] independent waist yaw enabled, cap "
                                  f"{math.degrees(lo):.0f}..{math.degrees(hi):.0f} deg, "
                                  f"slew {WAIST_SLEW_RAD_S} rad/s")
                        self._waist_active = True
                        self.last_waist = {"cmd": round(float(wq), 5),
                                           "q": (round(wm, 5) if wm is not None else None),
                                           "clamped": bool(waist_clamped),
                                           "leashed": bool(wleash)}
                        self.arm.set_waist_target(wq)
                        self.last_waist_seq = seq
                        self.n_waist_applied = getattr(self, "n_waist_applied", 0) + 1
                        return True
                    except Exception as exc:  # never let waist stop arm control
                        print("[onboard] waist command error:", exc)
                        self.last_waist_refusal = f"error: {exc}"[:80]
        self.n_waist_refused = getattr(self, "n_waist_refused", 0) + 1
        return False

    def apply_waist_velocity(self, velocity, *, velocity_clamped=False, seq=None, admission=None):
        """Update rate intent; the LowCmd loop alone integrates position."""
        self.last_waist_refusal = None
        if not self.deadman_ok():
            self.last_waist_refusal = "deadman"
        else:
            with self.lock:
                refusal = self._target_admission_refusal(admission)
                if refusal:
                    self.last_waist_refusal = refusal
                elif self.state != "ARMED":
                    self.last_waist_refusal = "not_armed"
                elif getattr(self, "homing", False):
                    self.last_waist_refusal = "homing"
                elif self.arm is None:
                    self.last_waist_refusal = "no_arm"
                else:
                    try:
                        self.arm.set_waist_velocity(
                            velocity, limits=effective_waist_limits(),
                            tracking_error=MAX_TRACKING_ERROR_RAD,
                            timeout_s=0.15,
                            max_velocity=WAIST_VELOCITY_MAX_RAD_S)
                        state = self.arm.waist_control_state()
                        measured = self.measured_waist_q()
                        command = state.get("target")
                        self._waist_active = True
                        self._waist_last_cmd = command
                        self.last_waist = {
                            "cmd": (round(float(command), 5)
                                    if command is not None else None),
                            "q": (round(measured, 5) if measured is not None else None),
                            "velocity": round(float(velocity), 5),
                            "clamped": bool(velocity_clamped or state.get("clamped")),
                            "leashed": bool(state.get("leashed")),
                            "stale": bool(state.get("stale")),
                        }
                        self.last_waist_seq = seq
                        self.n_waist_applied = getattr(self, "n_waist_applied", 0) + 1
                        return True
                    except Exception as exc:
                        print("[onboard] waist velocity error:", exc)
                        self.last_waist_refusal = f"error: {exc}"[:80]
        self.n_waist_refused = getattr(self, "n_waist_refused", 0) + 1
        return False

    def apply(self, arm14, left7, right7, waist=None, waist_clamped=False,
              seq=None, admission=None):
        """Compatibility path that commands both halves synchronously.

        Direct callers and hands-only mode retain the old all-or-nothing call.
        The live arm+hand websocket uses ``apply_arm`` and ``apply_hands`` on
        independent workers instead.
        """
        applied = self.apply_arm(arm14, waist=waist,
                                 waist_clamped=waist_clamped, admission=admission)
        if not applied:
            return False
        if not self.apply_hands(left7, right7, seq=seq, admission=admission):
            self.last_refusal = self.last_hand_refusal
            return False
        return True

    # ---- per-joint stiffness ----------------------------------------------
    @staticmethod
    def _gain_group(idx):
        j = ARM_JOINT_ORDER[idx]
        return "wrist" if "wrist" in j else ("elbow" if "elbow" in j else "shoulder")

    @classmethod
    def gain_bounds(cls):
        """{joint_name: {"kp": [0, max], "kd": [0, max]}} for the panel."""
        return {n: {"kp": [0.0, GAIN_KP_MAX[cls._gain_group(i)]],
                    "kd": [0.0, GAIN_KD_MAX]}
                for i, n in enumerate(ARM_JOINT_ORDER)}

    def _init_gains(self, arm):
        """Caller holds the lock. Seed from the controller, then a saved file."""
        if hasattr(arm, "set_arm_position_limits"):
            arm.set_arm_position_limits(effective_arm_limits())
        if hasattr(arm, "set_arm_motion_limits"):
            arm.set_arm_motion_limits(self.motion_limits)
        default = getattr(arm, "default_gains", None)
        self.gains = list(default()) if default else None
        if self.gains is None:
            return
        if os.environ.get("G1_ARM_GAIN_PROFILE", "saved") == "smooth_v1":
            self.gains = [(40.0, 3.0) if "wrist" in name else (80.0, 5.0)
                          for name in ARM_JOINT_ORDER]
            self._apply_gains_now(arm, self.gains)
            print("[onboard] smooth_v1 physical gains; saved table preserved for rollback")
            return
        saved = _load_gains()
        if saved is not None:
            self.gains = saved
            self._apply_gains_now(arm, saved)
            print(f"[onboard] loaded per-joint gains from {GAINS_PATH}")

    @staticmethod
    def _apply_gains_now(arm, gains):
        setter = getattr(arm, "set_joint_gains", None)
        if setter is None:
            return
        for i, (kp, kd) in enumerate(gains):
            setter(i, kp, kd)

    def _restore_gains(self):
        """Caller holds the lock. After limp: back to the commander's table."""
        arm = self.arm
        if arm is None:
            return
        if self.gains is not None and getattr(arm, "set_joint_gains", None):
            self._apply_gains_now(arm, self.gains)
        else:
            arm.set_arm_compliance()

    def set_motion_limits(self, limits):
        try:
            values = validate_motion_limits(limits)
        except ValueError as exc:
            return f"refused: {exc}"
        with self.lock:
            if self.state not in ("STANDBY", "HOLDING") or self.homing:
                return "refused: motion limits require STANDBY or HOLDING with no homing move"
            if self.arm is not None and not hasattr(self.arm, "set_arm_motion_limits"):
                return "refused: controller does not support live motion limits"
            # Persist before applying: a failed save must not look successful.
            path = MOTION_LIMITS_PATH + ".tmp"
            try:
                os.makedirs(os.path.dirname(MOTION_LIMITS_PATH), exist_ok=True)
                with open(path, "w") as f:
                    json.dump(values, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(path, MOTION_LIMITS_PATH)
            except OSError:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                return "refused: could not save motion limits; nothing changed"
            if self.arm is not None:
                self.arm.set_arm_motion_limits(values)
            self.motion_limits = values
            return "motion limits applied and saved"

    def set_gains(self, joints, ramp_s=GAIN_RAMP_S):
        """Set kp/kd for named arm joints, bounded, ramped. -> reply string.

        Blocking for `ramp_s`; the caller runs it off the event loop like the
        other commands. A second call during a ramp simply starts a new ramp
        from wherever the first had got to.
        """
        if not isinstance(joints, dict) or not joints:
            return "refused: gains needs {joint_name: {kp, kd}}"
        with self.lock:
            arm = self.arm
            if arm is None:
                return "refused: take_control first"
            if self.state == "LIMP":
                return "refused: hold first; gains are meaningless while limp"
            if self.state == "ARMED":
                # Stiffness is not adjustable on a moving arm.
                #
                # kp multiplies the standing position error, so changing it
                # while the arm is tracking an operator is a torque transient
                # at the one moment nobody is looking at the panel. The 0.5 s
                # ramp softens it and the bounds keep it survivable, but
                # "survivable" is the wrong bar for something that costs
                # nothing to avoid: the jog panel's own flow already passes
                # through HOLDING.
                return ("refused: hold first; stiffness cannot change while "
                        "ARMED — kp scales the standing tracking error, so a "
                        "change under load is a torque step")
            if self.gains is None:
                default = getattr(arm, "default_gains", None)
                if default is None:
                    return "refused: this controller cannot set gains per joint"
                self.gains = list(default())
            start = list(self.gains)
            new = list(self.gains)
            for name, g in joints.items():
                i = ARM_JOINT_INDEX.get(name)
                if i is None:
                    return f"refused: unknown arm joint {name!r}"
                try:
                    kp = float(g.get("kp", new[i][0]))
                    kd = float(g.get("kd", new[i][1]))
                except (AttributeError, TypeError, ValueError):
                    return f"refused: {name} gains must be numbers"
                if not (math.isfinite(kp) and math.isfinite(kd)):
                    return f"refused: {name} gains must be finite"
                kp_max = GAIN_KP_MAX[self._gain_group(i)]
                if not (0.0 <= kp <= kp_max):
                    return f"refused: {name} kp {kp:g} outside [0, {kp_max:g}]"
                if not (0.0 <= kd <= GAIN_KD_MAX):
                    return f"refused: {name} kd {kd:g} outside [0, {GAIN_KD_MAX:g}]"
                new[i] = (kp, kd)
            self.gains = new
        changed = [i for i in range(ARM_DIM) if new[i] != start[i]]
        if not changed:
            return "gains unchanged"
        steps = max(1, GAIN_RAMP_STEPS)
        for k in range(1, steps + 1):
            f = k / steps
            with self.lock:
                if self.arm is not arm or self.gains is not new:
                    return "gains superseded"        # relax(), or a newer call
                for i in changed:
                    arm.set_joint_gains(i,
                                        start[i][0] + f * (new[i][0] - start[i][0]),
                                        start[i][1] + f * (new[i][1] - start[i][1]))
            if k < steps:
                time.sleep(ramp_s / steps)
        names = [ARM_JOINT_NAMES[i] for i in changed]
        return f"gains set on {len(changed)} joint(s) over {ramp_s:.1f}s: {names}"

    def gains_save(self):
        with self.lock:
            g = self.gains
        if g is None:
            return "refused: no gains to save; take_control first"
        try:
            os.makedirs(os.path.dirname(GAINS_PATH) or ".", exist_ok=True)
            with open(GAINS_PATH, "w") as fh:
                json.dump({"gains": [list(x) for x in g]}, fh)
        except OSError as e:
            return f"refused: could not save gains: {e}"
        return f"gains saved to {GAINS_PATH}"

    def gains_clear(self):
        """Forget the saved file and ramp back to the controller defaults."""
        try:
            os.remove(GAINS_PATH)
        except OSError:
            pass
        with self.lock:
            arm = self.arm
        if arm is None or getattr(arm, "default_gains", None) is None:
            return "saved gains cleared"
        default = list(arm.default_gains())
        return self.set_gains({n: {"kp": default[i][0], "kd": default[i][1]}
                               for i, n in enumerate(ARM_JOINT_ORDER)})

    def gains_report(self):
        with self.lock:
            g = self.gains
        if g is None:
            return None
        return {n: {"kp": round(g[i][0], 2), "kd": round(g[i][1], 3)}
                for i, n in enumerate(ARM_JOINT_ORDER)}

    def measured_waist_q(self):
        """Motor 12 from the body slice (BODY_IDX starts at 12), or None."""
        body = getattr(self, "measured_body", None)
        if not body:
            return None
        idx, q = body.get("idx") or [], body.get("q") or []
        if idx and q and idx[0] == WAIST_MOTOR:
            return float(q[0])
        return None

    def _waist_step(self, target, now):
        """Slew, then leash, the waist yaw command. Caller must hold self.lock.

        Same shape as the arm's _slew + _leash, at the waist's own rate: first
        frame starts from the MEASURED waist (or the target itself when the
        onboard has no body state -- then the first command is the target,
        which the bridge has already seeded from measurement), a long gap
        licenses no long step, and the command is kept within the arm's
        tracking leash of the measured motor so a blocked waist cannot run
        away from a still body.
        """
        measured = self.measured_waist_q()
        if self._waist_last_cmd is None:
            self._waist_last_cmd = float(target) if measured is None else measured
            self._waist_last_t = now
        dt = min(0.1, max(1e-3, now - self._waist_last_t))
        step = WAIST_SLEW_RAD_S * dt
        q = min(self._waist_last_cmd + step, max(self._waist_last_cmd - step, float(target)))
        leashed = False
        if measured is not None:
            lo, hi = measured - MAX_TRACKING_ERROR_RAD, measured + MAX_TRACKING_ERROR_RAD
            held = min(hi, max(lo, q))
            leashed = held != q
            q = held
        self._waist_last_cmd = q
        self._waist_last_t = now
        return q, measured, leashed

    def _slew(self, arm, q_target, now):
        """Rate-limit the commanded pose. Caller must hold self.lock."""
        if self._last_cmd is None:
            # First frame of this ARMED session: start from the measured pose.
            self._last_cmd = np.asarray(arm.get_current_dual_arm_q(), float)
            self._last_cmd_t = now
        # Clamp dt: a long gap must not license a correspondingly long step.
        dt = min(0.1, max(1e-3, now - self._last_cmd_t))
        step = self.motion_limits["max_velocity_rad_s"] * dt
        q = np.clip(q_target, self._last_cmd - step, self._last_cmd + step)
        self._last_cmd = q
        self._last_cmd_t = now
        return q

    def merge_partial_arm(self, partial):
        """Named joints laid over the pose the arms were HELD at.

        Joints the sender did not name keep the value captured when control was
        taken -- they are not re-derived, not zeroed, and not carried forward
        from a previous frame. A frame that names nothing commands nothing new.
        """
        if not isinstance(partial, dict):
            raise ValueError("arm_partial must be an object")
        with self.lock:
            base = self._hold_pose
        if base is None:
            base = self.measured_arm_q
        if base is None:
            raise ValueError("no held pose yet; take_control first")
        out = list(base)
        for name, v in partial.items():
            i = ARM_JOINT_INDEX.get(name)
            if i is None:
                raise ValueError(f"unknown arm joint {name!r}")
            f = float(v)
            if not math.isfinite(f):
                raise ValueError(f"{name} is not finite")
            out[i] = f
        return sanitize_arm(out)

    def _leash(self, q, now):
        """Keep the command within MAX_TRACKING_ERROR_RAD of the measured pose.

        Caller must hold self.lock.
        """
        # The dashboard sample is decimated (~33 ms). At higher arm speeds
        # that age alone can look like tracking error. Match the motor writer:
        # use the controller's latest lowstate buffer without widening the leash.
        m = self.measured_arm_q
        if m is None:
            self._leash_hit = []
            return q
        arm = getattr(self, "arm", None)
        if arm is not None:
            m = arm.get_current_dual_arm_q()
        m = np.asarray(m, float)
        self._track_err.append(float(np.abs(q - m).max()))
        # Both vectors, for following_report. Recorded here because this is the
        # one place that holds the command and the measurement at the same
        # instant. Created on first use rather than assumed: Commander is built
        # by __new__ in several places (tests, take_hands) that never run
        # __init__, and a diagnostic must not be the thing that breaks them.
        self.__dict__.setdefault(
            "_cmd_hist", collections.deque(maxlen=150)).append(
                np.asarray(q, float).copy())
        self.__dict__.setdefault(
            "_meas_hist", collections.deque(maxlen=150)).append(m.copy())
        if len(self._track_err) > 300:
            del self._track_err[:-300]

        band = leash_band(getattr(self, "measured_arm_t", None), time.monotonic())
        out = np.clip(q, m - band, m + band)
        hit = np.nonzero(out != q)[0]
        # Which joints, this frame, for the ack. Set on every call so a frame
        # that needed no leash reads as none rather than as the last one that did.
        self._leash_hit = [_ARM_NAME_BY_INDEX[i] for i in hit]
        if hit.size:
            self.n_leashed += 1
            # The chain has to be corrected too. _slew starts from _last_cmd, so
            # leaving the unclamped value there would let the command keep
            # walking away and make this clamp fight a runaway every frame.
            self._last_cmd = out
            if now - self._leash_log_t > 2.0:
                self._leash_log_t = now
                names = [ARM_JOINT_NAMES[i] for i in hit[:4]]
                print(f"[onboard] tracking leash: {hit.size} joint(s) clamped at "
                      f"{math.degrees(MAX_TRACKING_ERROR_RAD):.0f} deg — {names}"
                      f"{' …' if hit.size > 4 else ''}")
        return out

    def _freeze(self):
        """Re-command the measured pose so the arm holds still.

        Caller must hold self.lock: this and apply() both drive the same
        controller from different threads.
        """
        # getattr, not attribute access: Commander is built by __new__ in
        # several paths that never run __init__, and freezing must never be the
        # thing that raises.
        arm = getattr(self, "arm", None)
        if arm is None:
            return
        self._last_cmd = None        # re-seed from measurement on resume
        self._waist_last_cmd = None  # the waist chain too; it has its own
        try:
            if hasattr(arm, "stop_waist_velocity"):
                arm.stop_waist_velocity(hold_measured=True)
            q = arm.get_current_dual_arm_q()
            if hasattr(arm, "hold_arm_measured"):
                arm.hold_arm_measured()
            else:
                arm.ctrl_dual_arm(q, np.zeros(ARM_DIM))
        except Exception as e:  # noqa: BLE001
            print("[onboard] freeze error:", e)
            return
        # The pose a partial frame is laid over is THIS one now, not the one
        # control was taken at. _hold_pose used to be captured once, in
        # take_control, and never again -- so after any freeze (watchdog,
        # hold, limp then hold) the arm sat here while merge_partial_arm kept
        # filling the unnamed joints from the old pose, and the next forearm
        # frame commanded the shoulders back to where control was taken, at
        # slew speed, under the leash. A move nobody asked for, at the moment
        # of re-arming. Every freeze path funnels through here, which is why
        # this is the place to refresh it.
        self._hold_pose = [float(v) for v in q]

    def _watchdog(self):
        """Return home if teleop frames stop; physical stop still takes priority.

        NOTE, and it matters: this detects that frames STOPPED, not that the
        operator stopped intending to move. A sender stuck in a loop
        re-transmitting the last pose satisfies it indefinitely -- which is why
        the sink refuses to re-send a stale pose rather than relying on this.

        With --deadman this runs alongside a positive hold on the robot's own
        remote. With --no-deadman it is the ONLY automatic stop, and the human
        stop is the remote's built-in damping combo, which cuts motion in the
        robot's motion-control service, below this process entirely.
        """
        was_held = True
        while not self._stop:
            held = self.deadman_ok()
            recover_reason = None
            with self.lock:
                if self.state == "ARMED" and not held:
                    if was_held:
                        print("[onboard] DEAD-MAN RELEASED -> freezing at current pose")
                        self._freeze()
                    self.state = "HOLDING"
                elif self.state == "ARMED" and time.time() - self.last_frame_t > DEAD_MAN_S:
                    gap = time.time() - self.last_frame_t
                    self.n_watchdog += 1
                    recover_reason = f"liveness: no applied frame for {gap:.2f}s"
            if recover_reason:
                self.recover_input_loss(recover_reason)
            was_held = held
            time.sleep(0.02)


def _strict_json(data):
    """Parse a frame, refusing NaN and Infinity.

    json.loads accepts bare NaN by default, and np.asarray happily carries it
    to a motor command. Refuse it at the door.
    """
    return json.loads(data, parse_constant=_reject_nonfinite)


class FeedbackOutbox:
    """Ordered, bounded receipts; a stalled socket cannot retain infinite poses.

    Source age plus local queue residence is carried without comparing host
    wall clocks. It excludes network transit, so is a lower bound at the Mac.
    Receipts are never silently dropped. Backpressure pauses target consumers
    while the input mailbox continues to replace intent. A failed write closes
    the owner socket and uses the existing disconnect recovery path.
    """

    def __init__(self, capacity=32, write_timeout=0.25, max_age=0.15):
        self.queue = asyncio.Queue(maxsize=capacity)
        self.write_timeout = write_timeout
        self.max_age = max_age

    async def put(self, item):
        await self.queue.put((time.monotonic(), item))

    def put_nowait(self, item):
        # The control reader must not wait behind feedback to receive STOP.
        # A full command-reply queue closes the socket and revokes ownership.
        self.queue.put_nowait((time.monotonic(), item))

    async def send_next(self, ws, cmd):
        queued_at, item = await self.queue.get()
        age = max(0.0, time.monotonic() - queued_at)
        if age > self.max_age:
            raise TimeoutError("control feedback queue expired")
        item["feedback_queue_age_s"] = age
        source_age = item.get("q_age_s")
        if isinstance(source_age, (int, float)) and math.isfinite(source_age):
            item["q_age_s"] = source_age + age
        reference = item.get("arm_reference")
        if isinstance(reference, dict) and isinstance(reference.get("age_s"), (int, float)):
            item["arm_reference"] = dict(reference, age_s=reference["age_s"] + age)
        item["control_safety"] = control_safety_status(cmd)
        await asyncio.wait_for(ws.send_json(item), timeout=self.write_timeout)


class LatestTargetMailbox:
    """Single-slot target mailbox with ordered supersession receipts.

    The WebSocket reader must never make the oldest frame in a buffered burst
    win. It overwrites this slot while the motor path is busy; the worker acks
    each displaced sequence as ``superseded`` and applies only the newest.
    Control commands do not enter this mailbox and therefore are never dropped.
    """

    def __init__(self, max_superseded=1024):
        self._latest = None
        self._superseded = []
        self._ready = asyncio.Event()
        self._max_superseded = max_superseded

    def put(self, frame):
        if self._latest is not None:
            if len(self._superseded) >= self._max_superseded:
                raise OverflowError("target receipt history exceeded its bound")
            self._superseded.append(self._latest)
        self._latest = frame
        self._ready.set()

    def clear(self):
        """Invalidate pending intent across an operator control transition."""
        self._latest = None
        self._superseded = []
        self._ready.clear()

    async def get(self, *, delay=None):
        while True:
            await self._ready.wait()
            # Leave the slot available for replacement until apply_arm can
            # accept it. Consuming first and sleeping would keep an older
            # frame while a newer intent waits behind it.
            remaining = delay() if delay is not None else 0.0
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            # No await in this block: put() cannot interleave between reading
            # the slot and clearing the event on the same event loop.
            latest = self._latest
            superseded = self._superseded
            self._latest = None
            self._superseded = []
            self._ready.clear()
            if latest is not None:
                return superseded, latest


def _note_target_arrival(cmd, frame):
    """Count every received target even when a newer one supersedes it."""
    cmd.last_rx_t = time.time()
    cmd.n_rx += 1
    ts = frame.get("t_sent")
    if isinstance(ts, (int, float)):
        cmd.note_rx_age(time.time() - float(ts))


def _prepare_target_frame(cmd, frame):
    """Validate a complete pose before either independent worker sees it.

    Arm and hand publishing are decoupled for latency, but validation remains
    atomic: a malformed arm must not let the hand half move, or vice versa.
    The event-loop caller does only finite/range checks and list construction;
    DDS writes stay in worker threads.
    """
    try:
        if "arm" in frame:
            arm = sanitize_arm(frame["arm"])
        elif "arm_partial" in frame:
            arm = cmd.merge_partial_arm(frame["arm_partial"])
        else:
            arm = None
        clamped_idx = list(LAST_CLAMPED["joints"]) if arm is not None else None
        left = sanitize_hand(frame.get("hand_left", [0.0] * HAND_DIM), "left")
        right = sanitize_hand(frame.get("hand_right", [0.0] * HAND_DIM), "right")
        waist_detached = bool(frame.get("waist_detached"))
        waist = (sanitize_waist(frame["waist"])
                 if "waist" in frame and arm is not None and not waist_detached
                 else None)
        waist_clamped = (bool(LAST_WAIST_CLAMPED["clamped"])
                         if waist is not None else False)
    except (TypeError, ValueError) as exc:
        cmd.n_rejected += 1
        print("[onboard] rejected target:", exc)
        return None
    return {"seq": frame.get("seq"), "arm": arm, "left": left,
            "admission": (getattr(cmd, "_target_epoch", 0), time.monotonic()),
            "right": right, "waist": waist,
            "waist_detached": waist_detached,
            "waist_clamped": waist_clamped,
            "clamped_idx": clamped_idx,
            # Old/fake commanders keep the combined path. Production and the
            # latency regression fake expose the two explicit methods.
            "split": (arm is not None and SEND_HANDS
                      and getattr(cmd, "hand", None) is not None
                      and hasattr(cmd, "apply_arm")
                      and hasattr(cmd, "apply_hands"))}


def _apply_target_frame(cmd, target):
    """Compatibility path: apply both halves on one worker."""
    applied = cmd.apply(target["arm"], target["left"], target["right"],
                        waist=target["waist"],
                        waist_clamped=target["waist_clamped"],
                        seq=target["seq"], **_admission_kwargs(cmd, target))
    ack = ack_message(cmd, target["seq"], applied, target["clamped_idx"])
    ack["waist_applied"] = target["waist"] is not None
    return ack


def _apply_arm_target_frame(cmd, target):
    """Apply only arm/waist; Dex3 is owned by the hand worker."""
    applied = cmd.apply_arm(target["arm"], waist=target["waist"],
                            waist_clamped=target["waist_clamped"],
                            **_admission_kwargs(cmd, target))
    ack = ack_message(cmd, target["seq"], applied, target["clamped_idx"])
    # A new bridge sends motor 12 through its independent mailbox. Older
    # bridges omit waist_detached and retain the combined behavior.
    ack["waist_applied"] = target["waist"] is not None
    ack["hand_target"] = {
        "queued": True,
        "last_applied_seq": getattr(cmd, "last_hand_seq", None),
    }
    return ack


def _prepare_waist_target_frame(cmd, frame):
    """Validate one independent motor-12 target on the websocket event loop."""
    if frame.get("waist_velocity_only") is True:
        try:
            raw = frame["waist_velocity"]
            velocity = sanitize_waist_velocity(raw)
        except (KeyError, TypeError, ValueError) as exc:
            cmd.n_rejected += 1
            print("[onboard] rejected waist velocity:", exc)
            return None
        return {"seq": frame.get("waist_seq"), "mode": "velocity",
                "admission": (getattr(cmd, "_target_epoch", 0), time.monotonic()),
                "velocity": velocity, "velocity_clamped": velocity != float(raw)}
    try:
        target = sanitize_waist(frame["waist"])
    except (KeyError, TypeError, ValueError) as exc:
        cmd.n_rejected += 1
        print("[onboard] rejected waist target:", exc)
        return None
    return {"seq": frame.get("waist_seq"), "mode": "position", "waist": target,
            "admission": (getattr(cmd, "_target_epoch", 0), time.monotonic()),
            "waist_clamped": bool(LAST_WAIST_CLAMPED["clamped"])}


def _apply_waist_target_frame(cmd, target, superseded):
    """Update the existing arm publisher's waist scalar on its own worker."""
    if target.get("mode") == "velocity":
        applied = cmd.apply_waist_velocity(
            target["velocity"], velocity_clamped=target["velocity_clamped"],
            seq=target["seq"], **_admission_kwargs(cmd, target))
    else:
        applied = cmd.apply_waist(target["waist"],
                                  waist_clamped=target["waist_clamped"],
                                  seq=target["seq"], **_admission_kwargs(cmd, target))
    return {
        "waist_ack": target["seq"],
        "state": cmd.state,
        "applied": bool(applied),
        "refused": (None if applied
                    else getattr(cmd, "last_waist_refusal", None)),
        "superseded": [old.get("seq") for old in superseded],
        "waist": getattr(cmd, "last_waist", None),
    }


def _admission_kwargs(cmd, target):
    # Minimal legacy/fake commanders do not have the admission contract.
    return {"admission": target.get("admission")} if isinstance(cmd, Commander) else {}


def _apply_hand_target_frame(cmd, target, superseded):
    """Apply one newest Dex3 target and return its separate receipt."""
    applied = cmd.apply_hands(target["left"], target["right"],
                              seq=target["seq"], **_admission_kwargs(cmd, target))
    return {
        "hand_ack": target["seq"],
        "state": cmd.state,
        "applied": bool(applied),
        "refused": None if applied else getattr(cmd, "last_hand_refusal", None),
        "superseded": [old.get("seq") for old in superseded],
    }


def control_safety_status(cmd):
    with cmd.lock:
        seen = getattr(cmd, "remote_t", 0.)
        age = time.time()-seen if seen else None
        bit = getattr(cmd, "deadman_bit", None)
        return {"configured": bit is not None,
                "button": next((name for name, value in REMOTE_BITS.items() if value == bit), None),
                "held": bool(cmd.deadman_ok()) if hasattr(cmd, "deadman_ok") else False,
                "fresh": age is not None and 0 <= age < REMOTE_STALE_S,
                "age_s": round(age,3) if age is not None else None,
                "release_required": not getattr(cmd, "_release_seen", False)}


async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    cmd: Commander = request.app["cmd"]
    secret = request.app["secret"]

    # Shared secret, if one was set. Without this the port is open to anything
    # that can route to the robot.
    if secret and request.query.get("key") != secret:
        print("[onboard] REFUSED a connection with a bad or missing key")
        await ws.send_json({"error": "bad key"})
        await ws.close()
        return ws

    # Exactly one operator. A second connection may watch, but it cannot
    # command, and -- crucially -- its disconnect must not release the arms.
    token = cmd.claim()
    if token is None:
        print("[onboard] REFUSED a second operator; one is already in control")
        await ws.send_json({"error": "another operator holds control",
                            "state": cmd.state})
        await ws.close()
        return ws

    cmd.reset_rx_age()          # the queue minimum belongs to this socket
    print("[onboard] operator connected; state =", cmd.state)

    # One writer. Acks, command replies and the idle stream all go through a
    # queue so two coroutines never write the same socket at once.
    out = FeedbackOutbox()
    last_out = [0.0]
    targets = LatestTargetMailbox()
    hand_targets = LatestTargetMailbox()
    waist_targets = LatestTargetMailbox()

    async def writer():
        try:
            while True:
                await out.send_next(ws, cmd)
        except (TimeoutError, ConnectionError, RuntimeError):
            await ws.close(code=1011, message=b"control feedback write stalled")

    async def idle_stream():
        """Measured joints at IDLE_Q_HZ whenever no ack has gone out lately, so
        a jog panel shows a live arm while the operator is not sending."""
        period = 1.0 / IDLE_Q_HZ
        while True:
            await asyncio.sleep(period)
            if time.monotonic() - last_out[0] >= period:
                await out.put(idle_q_message(cmd))
                last_out[0] = time.monotonic()

    async def target_worker():
        while True:
            superseded, newest = await targets.get(delay=lambda: max(
                0.0, min(1.0 / MAX_FRAME_HZ,
                         1.0 / MAX_FRAME_HZ - (time.time() - getattr(cmd, "last_apply_t", 0.0)))))
            if superseded:
                cmd.n_superseded += len(superseded)
                # It was never applied, so there is no per-target measured
                # state to report. Building the full body/temperature/Dex3
                # snapshot here made a burst of stale frames consume more CPU
                # than the one newest frame we actually need to command.
                # One batch also avoids N websocket JSON writes for a burst.
                await out.put({"superseded_acks": [old.get("seq") for old in superseded],
                               "state": cmd.state})
                last_out[0] = time.monotonic()
            apply_fn = (_apply_arm_target_frame if newest.get("split")
                        else _apply_target_frame)
            ack = await asyncio.to_thread(apply_fn, cmd, newest)
            if ack is not None:
                await out.put(ack)
                last_out[0] = time.monotonic()

    async def hand_worker():
        """Publish Dex3 at its sustainable rate without occupying arm/waist."""
        while True:
            superseded, newest = await hand_targets.get()
            cmd.n_hand_superseded = (getattr(cmd, "n_hand_superseded", 0)
                                     + len(superseded))
            receipt = await asyncio.to_thread(
                _apply_hand_target_frame, cmd, newest, superseded)
            await out.put(receipt)
            last_out[0] = time.monotonic()

    async def waist_worker():
        """Refresh motor 12 independently from arm IK and Dex3 workers."""
        while True:
            superseded, newest = await waist_targets.get()
            cmd.n_waist_superseded = (getattr(cmd, "n_waist_superseded", 0)
                                      + len(superseded))
            receipt = await asyncio.to_thread(
                _apply_waist_target_frame, cmd, newest, superseded)
            await out.put(receipt)
            last_out[0] = time.monotonic()

    writer = asyncio.create_task(writer())
    idle_task = asyncio.create_task(idle_stream())
    target_task = asyncio.create_task(target_worker())
    hand_task = asyncio.create_task(hand_worker())
    waist_task = asyncio.create_task(waist_worker())
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                m = _strict_json(msg.data)
            except (json.JSONDecodeError, ValueError) as e:
                print("[onboard] rejected frame:", e)
                continue
            if not isinstance(m, dict):
                continue

            if "cmd" in m:
                name = m["cmd"]
                if name not in ("status", "speak"):
                    with cmd.lock:
                        cmd._target_epoch = getattr(cmd, "_target_epoch", 0) + 1
                    # Pending pre-stop/pre-arm intent cannot survive the new
                    # state. Control messages bypass the paced motor worker.
                    targets.clear()
                    hand_targets.clear()
                    waist_targets.clear()
                if name not in ("status", "speak") and getattr(cmd, "locomotion", None):
                    cmd.locomotion.reset()
                # Every one of these can block for seconds inside vendored code
                # (DDS handshakes, go-home retry loops). Run them off the event
                # loop: a blocking call here froze /status and every other
                # command, so the one moment an operator needed to say "hold"
                # was the one moment nothing could answer.
                if name == "status":
                    reply = ""       # read-only; the reply below carries state
                elif name == "relax":
                    reply = await asyncio.to_thread(
                        cmd.relax, bool(m.get("go_home", False)))
                elif name == "motion_limits":
                    reply = await asyncio.to_thread(cmd.set_motion_limits, m.get("limits"))
                elif name == "gains":
                    reply = await asyncio.to_thread(cmd.set_gains, m.get("joints"))
                elif name == "speak":
                    # queue_speech is non-blocking. The audio RPC runs on its
                    # own worker so speech cannot add latency to pose frames.
                    reply = cmd.queue_speech(m.get("prompt"))
                elif name == "home":
                    # With the closed hand pose from the bridge's grip, if sent.
                    reply = await asyncio.to_thread(cmd.go_home, m.get("hands"))
                else:
                    fn = {"take_control": cmd.take_control, "arm": cmd.arm_on,
                          "take_hands": cmd.take_hands,
                          "hold": cmd.hold,
                          "set_home": cmd.set_home,
                          # Press, walk to the robot, pose, hold still.
                          "set_home_in_10": lambda: cmd.set_home(10.0),
                          "set_home_in_30": lambda: cmd.set_home(30.0),
                          "limp": cmd.limp_arms,
                          "calibrate_start": cmd.calibrate_start,
                          "calibrate_stop": cmd.calibrate_stop,
                          "calibrate_cancel": cmd.calibrate_cancel,
                          "calibrate_clear": cmd.calibrate_clear,
                          "gains_save": cmd.gains_save,
                          "gains_clear": cmd.gains_clear}.get(name)
                    reply = (await asyncio.to_thread(fn) if fn
                             else f"unknown cmd {name}")
                if name != "status":
                    print("[onboard]", name, "->", reply)
                # Measured joint angles ride along with every status reply.
                #
                # An episode that records only what we COMMANDED is half a
                # demonstration: during a freeze, or any stretch where the arm
                # cannot keep up, commanded and measured diverge completely and
                # the commanded half is the one that keeps moving. Training on
                # it would teach the arm to do what it was told rather than
                # what it did.
                with cmd.lock:
                    q = cmd.measured_arm_q
                    measured_t = getattr(cmd, "measured_arm_t", None)
                out.put_nowait({
                    "state": cmd.state, "msg": reply,
                    "command_reply": {"request_id": m.get("request_id"), "command": name, "message": reply}
                        if name != "status" else None,
                    "locomotion": (cmd.locomotion.status()
                                   if getattr(cmd, "locomotion", None)
                                   else {"enabled": False}),
                    "homing": getattr(cmd, "homing", False),
                    "input_loss_recovery": getattr(cmd, "input_loss_recovery", None),
                    "motion_limits_reply": {"request_id": m.get("request_id"),
                        "ok": reply == "motion limits applied and saved",
                        "limits": dict(cmd.motion_limits), "message": reply}
                        if name == "motion_limits" else None,
                    "q": [round(float(v), 5) for v in q] if q is not None else None,
                    "q_age_s": (max(0.0, time.monotonic() - measured_t)
                                if measured_t is not None else None),
                    "gains": cmd.gains_report() if name in ("gains", "gains_save",
                                                            "gains_clear", "status") else None,
                })

            elif m.get("locomotion_only") is True:
                if getattr(cmd, "locomotion", None):
                    cmd.locomotion.submit(m.get("locomotion"))

            elif ((m.get("waist_velocity_only") is True and "waist_velocity" in m)
                  or (m.get("waist_only") is True and "waist" in m)):
                target = _prepare_waist_target_frame(cmd, m)
                if target is None:
                    continue
                cmd.n_waist_received = getattr(cmd, "n_waist_received", 0) + 1
                waist_targets.put(target)

            elif "arm" in m or "hand_left" in m:
                # Arrival is counted before newest-wins selection so status
                # distinguishes transport loss from deliberate supersession.
                _note_target_arrival(cmd, m)
                target = _prepare_target_frame(cmd, m)
                if target is None:
                    continue
                targets.put(target)
                if target["split"]:
                    cmd.n_hand_received = getattr(cmd, "n_hand_received", 0) + 1
                    hand_targets.put(target)
    except (OverflowError, asyncio.QueueFull):
        await ws.close(code=1013, message=b"target backlog exceeded")
    finally:
        # to_thread cancellation cannot stop a thread already queued in the
        # executor. Revoke its admission before releasing this owner socket.
        with cmd.lock:
            cmd._target_epoch = getattr(cmd, "_target_epoch", 0) + 1
        if getattr(cmd, "locomotion", None):
            cmd.locomotion.reset()
        waist_task.cancel()
        hand_task.cancel()
        target_task.cancel()
        idle_task.cancel()
        writer.cancel()
        # The robot owns the return: it must survive loss of the bridge too.
        # A second socket, or a disconnect after an explicit Hold, cannot move it.
        await asyncio.to_thread(cmd.operator_disconnected, token)
    return ws


async def status_handler(request):
    cmd: Commander = request.app["cmd"]
    with cmd.lock:
        raw, seen, held = cmd.remote_raw, cmd.remote_t, cmd.deadman_held
    age = time.time() - seen if seen else None
    arm = cmd.arm
    return web.json_response({
        "state": cmd.state,
        "locomotion": (cmd.locomotion.status()
                       if getattr(cmd, "locomotion", None) else {"enabled": False}),
        "motion_profile": {"name": "smooth_v1", "clock": "motor_publisher",
                           "gain_profile": os.environ.get("G1_ARM_GAIN_PROFILE", "saved"),
                           **cmd.motion_limits,
                           "hard_guards_override_smoothing": True},
        "lowstate_ok": cmd.lowstate_ok(),
        "input_loss_recovery": getattr(cmd, "input_loss_recovery", None),
        "input_loss_policy": input_loss_policy(),
        # The one lowstate reader: poll rate, samples actually taken per second
        # and the newest one's age. rate_hz near poll_hz and age_s ~5 ms is
        # healthy; the old listener's symptom was bursts 110-145 ms apart.
        "lowstate": (cmd.lowstate_hub.status()
                     if getattr(cmd, "lowstate_hub", None) is not None else None),
        "waist": getattr(cmd, "last_waist", None),
        "mode": ("hands-only (arms cannot move)" if cmd.hands_only
                 else "arms+hands" if cmd.arm is not None else "idle"),
        "home": {"captured": cmd.home_q is not None, "homing": cmd.homing,
                 "q": cmd.home_q, "measured_now": cmd.measured_arm_q},
        "calibration": {
            "recording": cmd.cal is not None,
            "source": "calibrated" if ARM_LIMITS_CAL else "mechanical (datasheet)",
            "limits": [list(x) for x in effective_arm_limits()],
            # In progress if recording, otherwise what is on disk.
            #
            # This used to expose the raw extremes ONLY while recording, so a
            # calibration backed up afterwards carried the limits and not the
            # measurements they came from -- and restoring it would hit exactly
            # the "cannot top up, starting from scratch" problem that cost a
            # full re-walk of both arms today.
            "observed": ({"lo": cmd.cal["lo"], "hi": cmd.cal["hi"]}
                         if cmd.cal else _load_cal_observed()),
        },
        "frames": {
            "applied": cmd.n_applied,
            "superseded": cmd.n_superseded,
            "rejected": cmd.n_rejected,
            "watchdog_freezes": cmd.n_watchdog,
            "times_armed": cmd.n_armed,
            "last_frame_age_s": (round(time.time() - cmd.last_frame_t, 3)
                                 if cmd.last_frame_t else None),
            "last_freeze_why": cmd.last_freeze_why,
            "last_rx_age_s": (round(time.time() - cmd.last_rx_t, 3)
                              if cmd.last_rx_t else None),
            "received": cmd.n_rx,
            "rx_age": cmd.rx_age_trend(),
            "lowstate_callbacks": cmd.n_lowstate,
        },
        "publisher": {
            "loop_hz": getattr(arm, "loop_hz", None),
            "target_hz": (round(1.0 / arm.control_dt, 1) if arm is not None else None),
            "worst_iteration_ms": getattr(arm, "loop_worst_ms", None),
            "crc_ms": getattr(arm, "crc_ms", None),
            "crc_backend": getattr(getattr(arm, "crc", None), "backend", None),
            "crc_parity_ok": getattr(getattr(arm, "crc", None), "parity_ok", None),
            "crc_errors": getattr(getattr(arm, "crc", None), "errors", None),
            "write_ms": getattr(arm, "write_ms", None),
            "record_ms": getattr(arm, "record_ms", None),
            "tick_interval_ms": getattr(arm, "tick_interval_ms", None),
            "deadlines_missed": getattr(arm, "deadlines_missed", None),
            "affinity": getattr(arm, "affinity", None),
            "arm_cmd_ms": cmd.arm_cmd_ms,
            "hand_cmd_ms": cmd.hand_cmd_ms,
            "send_hands": SEND_HANDS,
            "hand_targets": {
                "received": getattr(cmd, "n_hand_received", 0),
                "applied": getattr(cmd, "n_hand_applied", 0),
                "superseded": getattr(cmd, "n_hand_superseded", 0),
                "refused": getattr(cmd, "n_hand_refused", 0),
                "last_applied_seq": getattr(cmd, "last_hand_seq", None),
                "last_refusal": getattr(cmd, "last_hand_refusal", None),
            },
            "waist_targets": {
                "received": getattr(cmd, "n_waist_received", 0),
                "applied": getattr(cmd, "n_waist_applied", 0),
                "superseded": getattr(cmd, "n_waist_superseded", 0),
                "refused": getattr(cmd, "n_waist_refused", 0),
                "last_applied_seq": getattr(cmd, "last_waist_seq", None),
                "last_refusal": getattr(cmd, "last_waist_refusal", None),
            },
        },
        "motor_temperature": _motor_temperature_message(
            getattr(cmd, "measured_motor_temperature", None)),
        "native_recording": (cmd.telemetry.status()
                             if getattr(cmd, "telemetry", None) is not None else None),
        "arm_reference": _arm_reference_message(cmd),
        "gains": cmd.gains_report(),
        "gain_bounds": Commander.gain_bounds(),
        # The Dex3, beyond position: hottest motor, error words, current, and
        # which joints the stall back-off is holding short right now.
        "hands": _hand_summary(
            cmd.hand, getattr(cmd, "_hand_backoff_names", None),
            (getattr(getattr(cmd, "hand", None), "_thermal", None).status()
             if getattr(getattr(cmd, "hand", None), "_thermal", None) else None)),
        "dex3_gains": {"kp": getattr(cmd.hand, "kp", DEX3_KP), "kd": getattr(cmd.hand, "kd", DEX3_KD),
                       "stall_backoff": cmd._stall.enabled,
                       "stall_s": cmd._stall.stall_s, "stall_gap_rad": cmd._stall.gap_rad,
                       "hold_margin_rad": cmd._stall.margin_rad},
        "camera": (request.app.get("camera").status()
                   if request.app.get("camera") is not None else None),
        # Command vs measured: did the arm do what it was told?
        "tracking": cmd.tracking_report(),
        # Commanded vs actually moved -- catches "powered on but not in Main
        # Operation Control", which every other field reports as healthy.
        "following": cmd.following_report(),
        # Command vs joint limits: were we asked for something unreachable?
        "saturation": {
            "clamped_joints": LAST_CLAMPED["n"],
            "which": [ARM_JOINT_NAMES[i] for i in LAST_CLAMPED["joints"]],
            "note": ("most joints clamped every frame means the retargeter is "
                     "aiming outside the arm's reach"
                     if LAST_CLAMPED["n"] >= 7 else ""),
            "write_ms": getattr(arm, "write_ms", None),
        },
        "deadman": {
            "configured": cmd.deadman_bit is not None,
            "held": cmd.deadman_ok(),
            "raw": f"0x{raw:04x}",
            "bits": [n for n, b in REMOTE_BITS.items() if raw & (1 << b)],
            "lowstate_age_s": round(age, 3) if age is not None else None,
            "release_required": not cmd._release_seen,
        },
    })


def main():
    # The default 5 ms GIL switch interval lets a DDS/telemetry callback hold
    # the interpreter for half of a 100 Hz motor period. A bounded 0.5 ms
    # quantum favors command responsiveness; it is deploy-time reversible.
    switch_ms = float(os.environ.get("G1_PYTHON_SWITCH_MS", "0.5"))
    if not math.isfinite(switch_ms) or not 0.1 <= switch_ms <= 5.0:
        raise ValueError("G1_PYTHON_SWITCH_MS must be from 0.1 through 5.0")
    sys.setswitchinterval(switch_ms / 1000.0)

    ap = argparse.ArgumentParser()
    ap.add_argument("--net", required=True, help="network interface for DDS (e.g. eth0)")
    ap.add_argument("--port", type=int, default=8090)
    # Domain 0 is the REAL robot; 1 is the simulator. Defaulting to 1 means a
    # run with no flags cannot reach hardware, and the project's own rule --
    # nothing published to the real robot that has not run in sim first -- can
    # actually be followed with this binary rather than around it.
    ap.add_argument("--domain", type=int, default=1,
                    help="DDS domain: 1 = sim (default), 0 = REAL ROBOT")
    ap.add_argument("--key", default="", help="shared secret; connections must pass ?key=")
    ap.add_argument("--locomotion", action="store_true",
                    help="enable bounded left-stick walking while ARMED; off by default")
    ap.add_argument("--deadman", default="",
                    help="remote button that must be HELD for motion, e.g. R2. "
                         "Empty means no dead-man, which is refused unless --no-deadman.")
    ap.add_argument("--no-deadman", action="store_true",
                    help="run with no dead-man. Simulator only; refused on domain 0.")
    ap.add_argument("--deadman-discover", action="store_true",
                    help="log the remote's raw value as buttons change, to identify "
                         "which bit is which on THIS remote")
    args = ap.parse_args()

    if args.deadman and args.deadman not in REMOTE_BITS:
        ap.error(f"--deadman must be one of {sorted(REMOTE_BITS)}")
    if args.domain == 0 and not args.deadman and not args.no_deadman \
            and not args.deadman_discover:
        ap.error("refusing to run against the real robot with no dead-man. "
                 "Pass --deadman <button>, --deadman-discover to identify one, "
                 "or --no-deadman if you really mean it.")

    if args.domain == 0:
        print("[onboard] *** DDS DOMAIN 0 — THIS IS THE REAL ROBOT ***")
    if not args.key:
        print("[onboard] WARNING: no --key set; anything that can reach this port can command the arms")

    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
    from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_

    print(f"[onboard] initializing DDS on {args.net}, domain {args.domain} …")
    ChannelFactoryInitialize(args.domain, args.net)
    cmd = Commander()
    cmd.discover = args.deadman_discover
    cmd.deadman_bit = REMOTE_BITS.get(args.deadman) if args.deadman else None
    cmd.locomotion = None
    if args.locomotion and not args.deadman_discover:
        from g1_locomotion import Locomotion
        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
        loco_client = LocoClient()
        loco_client.SetTimeout(.1)
        loco_client.Init()
        cmd.locomotion = Locomotion(loco_client, cmd.locomotion_gate)
        cmd.locomotion.start()

    # Optional for control, required for A/B speech. Keep teleoperation alive
    # if an older SDK image lacks the client or the audio service is absent;
    # the command reply will report that speech is unavailable.
    try:
        from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
        audio = AudioClient()
        audio.SetTimeout(3.0)
        audio.Init()
        cmd.audio = audio
        print("[onboard] G1 audio TTS ready (English speaker 1)")
    except Exception as exc:  # noqa: BLE001 -- audio is not a motion dependency
        print(f"[onboard] G1 audio TTS unavailable: {exc}")

    # Read-only, and the process's ONLY reader of rt/lowstate (lowstate_hub.py):
    # no handler, so nothing is decoded until the hub takes the newest sample
    # at G1_LOWSTATE_HZ. The Commander and, after take_control, the arm
    # controller both read from it. This process publishes nothing on lowstate
    # and never will.
    from lowstate_hub import LowstateHub, poll_hz_from_env
    lowstate_sub = ChannelSubscriber("rt/lowstate", LowState_)
    lowstate_sub.Init()
    hub = LowstateHub(lowstate_sub.Read, hz=poll_hz_from_env())
    hub.add_listener(cmd.on_lowstate)
    cmd.lowstate_hub = hub
    hub.start()
    print(f"[onboard] lowstate: one reader, newest sample at {hub.hz:g} Hz (G1_LOWSTATE_HZ)")
    app_keepalive = lowstate_sub          # keep a reference alive

    if cmd.deadman_bit is not None:
        print(f"[onboard] DEAD-MAN: hold {args.deadman} on the robot's remote "
              f"(bit {cmd.deadman_bit}) for motion. Release it and the arms freeze.")
    elif args.domain == 0:
        print("[onboard] *** NO SOFTWARE DEAD-MAN. The stop is the remote's own "
              "damping combo. ***")
        print("[onboard] The operator's hands are tracked and cannot hold it — "
              "a SECOND PERSON must have the remote.")
    else:
        print("[onboard] no dead-man configured (simulator, domain 1).")
    if args.deadman_discover:
        print("[onboard] discovery mode: press buttons on the remote and watch this log")

    app = web.Application()
    app["cmd"] = cmd
    app["secret"] = args.key
    app["lowstate"] = app_keepalive
    async def stop_locomotion(app):
        if cmd.locomotion is not None:
            await asyncio.to_thread(cmd.locomotion.close)
    app.on_cleanup.append(stop_locomotion)
    app.router.add_get("/ctrl", ws_handler)
    app.router.add_get("/status", status_handler)
    # Passive, authenticated recording endpoints. JSON encoding is isolated
    # in a spawned worker process and only starts while a bridge subscribes.
    from raw_telemetry import readings_ws_handler, readings_clock_handler
    app.router.add_get("/readings", readings_ws_handler)
    app.router.add_get("/readings-clock", readings_clock_handler)
    # The head camera, for the episode recorder on the bridge. Its own socket
    # so nothing about the pose path changes; its own thread so a camera that
    # is missing, slow or unplugged costs a counter in /status and nothing
    # else. G1_CAM=0 leaves it out entirely.
    from g1_camera import CameraSource, cam_ws_handler
    camera = CameraSource.from_env()
    app["camera"] = camera
    if camera is not None:
        camera.start()
        app.router.add_get("/cam", cam_ws_handler)
        print(f"[onboard] camera {camera.device} {camera.width}x{camera.height} "
              f"@{camera.hz:g} Hz on /cam (G1_CAM=0 to disable)")
    else:
        print("[onboard] camera off (G1_CAM=0)")
    print(f"[onboard] STANDBY. WebSocket on :{args.port}/ctrl — operator must send take_control then arm.")
    print("[onboard] KEEP THE E-STOP IN REACH.")
    web.run_app(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
