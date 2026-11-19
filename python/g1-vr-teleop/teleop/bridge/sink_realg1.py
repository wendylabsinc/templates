"""RealG1Sink — laptop side of the real-robot path.

THE DEAD-MAN IS NOT HERE. It lives on the robot, reading the G1's own remote
from rt/lowstate a few microseconds before the motors, where nothing in this
process, the browser, the headset or the network can hold it down. This sink's
safety job is narrower and still essential: refuse malformed targets, and STOP
SENDING when the hand data goes stale, because the robot's liveness watchdog
can only fire if frames actually stop.

Takes the retargeter's {joint_name: rad} dict, extracts the 14 arm + 2x7 hand
targets, and streams them to the onboard commander (real/g1_onboard.py) over a
WebSocket (typically a Wendy tunnel URL). Motion is gated ONBOARD by the safety
state machine — this sink streams unconditionally; the robot ignores frames
unless a spotter has armed it.

Spotter control (typed at the laptop terminal — the software e-stop):
    take   -> take_control (grab arms, hold pose)
    arm    -> enable teleop motion
    hold   -> freeze at current pose
    stop   -> relax (release arms, relax hands)  [also on Ctrl-C / disconnect]

Runs its own thread + event loop so apply() (called from the bridge loop) just
writes the latest targets under a lock. NOT yet run on hardware.
"""

import asyncio
import collections
import json
import math
import os
import sys
import threading
import time

import aiohttp

from retarget_g1 import (ARM_LEFT, ARM_RIGHT, DEX3_LEFT, DEX3_RIGHT,
                         ROBOT_LIMITS)
from thermal_rise import ThermalRiseMonitor

_ARM = ARM_LEFT + ARM_RIGHT  # 14, matches G1_29_JointArmIndex order
_BODY_NAME_BY_INDEX = {
    12: "waist_yaw_joint", 13: "waist_roll_joint", 14: "waist_pitch_joint",
    **{i + 15: name for i, name in enumerate(_ARM)},
}

DEFAULT_SEND_HZ = 90.0
DEFAULT_WAIST_SEND_HZ = 100.0
MOTOR_HEAT_ALERT_C = 80.0


def _next_deadline(previous, period, now):
    """Advance an absolute clock to its first future tick, without bursts."""
    deadline = previous + period
    if deadline <= now:
        deadline += (math.floor((now - deadline) / period) + 1) * period
    return deadline


def send_hz_from_env(env=None):
    """Bridge-to-onboard target rate, aligned with the 50 Hz publisher.

    Sixty hertz leaves one fresh target available for almost every 50 Hz
    physical publish without the 100 Hz resend loop building bursts that the
    onboard then refuses.  Keep it bounded: below 20 Hz feels stepped and
    above 100 Hz recreates the measured overload.
    """
    env = os.environ if env is None else env
    raw = env.get("TELEOP_REALG1_SEND_HZ", str(DEFAULT_SEND_HZ))
    try:
        hz = float(raw)
    except (TypeError, ValueError):
        raise ValueError("TELEOP_REALG1_SEND_HZ must be a number") from None
    if not math.isfinite(hz) or not 20.0 <= hz <= 100.0:
        raise ValueError("TELEOP_REALG1_SEND_HZ must be between 20 and 100")
    return hz


def waist_send_hz_from_env(env=None):
    """Independent bridge-to-onboard motor-12 target refresh rate."""
    env = os.environ if env is None else env
    raw = env.get("TELEOP_REALG1_WAIST_SEND_HZ", str(DEFAULT_WAIST_SEND_HZ))
    try:
        hz = float(raw)
    except (TypeError, ValueError):
        raise ValueError("TELEOP_REALG1_WAIST_SEND_HZ must be a number") from None
    if not math.isfinite(hz) or not 20.0 <= hz <= 200.0:
        raise ValueError("TELEOP_REALG1_WAIST_SEND_HZ must be between 20 and 200")
    return hz


class RealG1Sink:
    # A frame older than this is not sent. This is what lets the onboard
    # dead-man work at all: it watches for frames STOPPING, and this sink used
    # to re-send the last pose forever, so frames never stopped and the
    # dead-man could never fire. Staleness has to be enforced by whoever knows
    # when the data was produced -- which is here, not on the robot.
    STALE_S = 0.15

    # The only arm joints a partial frame may carry: one side's elbow and three
    # wrist joints, or both sides'. Forearm mode drives exactly these; the
    # SHOULDER is never sent.
    #
    # Whole per-side SETS, not a loose subset. A frame carrying three of a
    # side's four is a frame that lost one, and holding the lost joint while
    # moving its neighbours is a move nobody asked for. Matching per-side keeps
    # one-handed tracking legitimate without opening that door.
    _FOREARM_SET = {s: frozenset({f"{s}_elbow_joint"}
                                 | {f"{s}_wrist_{j}_joint"
                                    for j in ("roll", "pitch", "yaw")})
                    for s in ("left", "right")}
    # With shoulder mode on, a side also carries pitch and roll, and -- since
    # the solve learned to yaw the shoulder inward instead of rolling the
    # upper arm into the torso -- yaw. A bridge with TELEOP_SHOULDER_YAW=0
    # still sends the six-joint shape, so both are legal.
    _SHOULDER_PR_SET = {s: frozenset(f"{s}_shoulder_{j}_joint"
                                     for j in ("pitch", "roll"))
                        for s in ("left", "right")}
    _SHOULDER_SET = {s: frozenset(f"{s}_shoulder_{j}_joint"
                                  for j in ("pitch", "roll", "yaw"))
                     for s in ("left", "right")}
    # The legal shapes per side: forearm alone, forearm plus pitch/roll, or
    # forearm plus all three shoulder axes. Still whole-set-or-nothing -- a
    # frame carrying six of a side's seven is a frame that lost one, and
    # holding the lost joint while its neighbours move is a move nobody asked
    # for.
    #
    # Written out rather than built by comprehension: a comprehension in a
    # CLASS BODY gets its own scope and cannot see the class attributes above
    # it, so the obvious form raises NameError at import.
    _ALLOWED = {
        "left": (_FOREARM_SET["left"],
                 _FOREARM_SET["left"] | _SHOULDER_PR_SET["left"],
                 _FOREARM_SET["left"] | _SHOULDER_SET["left"]),
        "right": (_FOREARM_SET["right"],
                  _FOREARM_SET["right"] | _SHOULDER_PR_SET["right"],
                  _FOREARM_SET["right"] | _SHOULDER_SET["right"]),
    }
    FOREARM_JOINTS = (_FOREARM_SET["left"] | _SHOULDER_SET["left"]
                      | _FOREARM_SET["right"] | _SHOULDER_SET["right"])

    @classmethod
    def _is_forearm_set(cls, have):
        """True only if `have` is a union of WHOLE per-side legal shapes."""
        have = set(have)
        if not have:
            return False
        rest = set(have)
        for side, shapes in cls._ALLOWED.items():
            touched = [sh for sh in shapes if sh & have]
            if not touched:
                continue
            # Largest matching shape first, so forearm+shoulder is preferred
            # over forearm alone when both could match.
            match = max((sh for sh in touched if sh <= have),
                        key=len, default=None)
            if match is None:
                return False           # a partial shape: a frame lost a joint
            rest -= match
        return not rest

    def __init__(self, url: str, send_hz: float | None = None,
                 waist_send_hz: float | None = None):
        self.url = url
        send_hz = send_hz_from_env() if send_hz is None else float(send_hz)
        if not math.isfinite(send_hz) or not 20.0 <= send_hz <= 100.0:
            raise ValueError("send_hz must be between 20 and 100")
        self.send_hz = send_hz
        self.dt = 1.0 / send_hz
        waist_send_hz = (waist_send_hz_from_env() if waist_send_hz is None
                         else float(waist_send_hz))
        if not math.isfinite(waist_send_hz) or not 20.0 <= waist_send_hz <= 200.0:
            raise ValueError("waist_send_hz must be between 20 and 200")
        self.waist_send_hz = waist_send_hz
        self.waist_dt = 1.0 / waist_send_hz
        self._latest = None
        self._latest_t = 0.0
        self._latest_waist = None
        self._latest_waist_t = 0.0
        self._lock = threading.Lock()
        self._cmd_q = []          # spotter commands, drained by the io loop
        self._cmd_lock = threading.Lock()
        # What the onboard commander last told us about itself. Without reading
        # its replies we could not tell ARMED from "refused: take_control
        # first", and the operator would be guessing.
        self.n_sent = 0            # frames actually put on the wire
        self.n_retargeted = 0      # frames that came out of the retargeter
        self.onboard_state = "?"
        self._locomotion_pending = None
        self.onboard_locomotion = None
        self.onboard_msg = ""
        self.onboard_q = None      # measured arm joints, newest from any reply
        self.onboard_body = None   # waist+arms q/dq/tau (BODY_IDX order), newest from any reply
        self.onboard_body_t = 0.0  # when it arrived (monotonic)
        self.onboard_motor_temperature = None  # all 29 active body motors
        self.onboard_motor_temperature_t = 0.0
        self.onboard_q_t = 0.0     # when it arrived (monotonic)
        self.onboard_gains = None  # {joint: {kp, kd}} when the onboard reports it
        self._arm_offset = None    # target-minus-measured, captured at arming
        self._warned_no_q = False
        self.seq = 0               # frame sequence number, echoed in acks
        self.waist_seq = 0         # independent motor-12 sequence number
        self.n_acked = 0           # frames the onboard said it READ
        self.n_applied_ack = 0     # frames the onboard said it COMMANDED
        self.last_ack = None
        self.last_applied = None   # the newest ack's applied flag
        self.n_hand_acked = 0      # completed independent Dex3 worker receipts
        self.n_hand_applied_ack = 0
        self.last_hand_ack = None
        self.last_hand_applied = None
        self.last_hand_refused = None
        self.last_hand_superseded = []
        self.last_hand_q = None    # measured Dex3 positions from the newest ack
        self.last_hand_state = None    # temperature/current/mode/error per hand, newest
        self._thermal = ThermalRiseMonitor()
        self.last_hand_backoff = None  # joints the onboard backed off as stalled, newest ack
        self.last_clamped = []     # joints the newest ack said hit a limit
        self.last_leashed = []     # joints the newest ack said the leash held
        self.last_waist = None     # the newest ack's waist {cmd, q, clamped, leashed}
        self.last_waist_t = 0.0    # bridge monotonic receipt time for freshness decisions
        self.n_waist_sent = 0
        self.n_waist_acked = 0
        self.n_waist_applied_ack = 0
        self.last_waist_ack = None
        self.last_waist_applied = None
        self.last_waist_refused = None
        self.last_waist_superseded = []
        self._last_said = None     # (state, msg) last printed: speak on change only
        self._last_hands = None    # last finger targets sent, for the manual source
        # Raw post-offset targets that did not fit this robot's calibrated
        # envelope.  The onboard already clamps these; keeping the decision on
        # the bridge too makes the orange pose honest and preserves the raw
        # request for replay instead of silently losing it.
        self._ik_rejections = collections.deque(maxlen=256)
        self._ik_rejection_v = 0
        self.n_ik_rejected = 0
        self.connected = False
        threading.Thread(target=self._thread, daemon=True).start()
        threading.Thread(target=self._stdin, daemon=True).start()

    # name -> index into the onboard's 14-vector
    _ARM_INDEX = {n: i for i, n in enumerate(_ARM)}

    def _offset_to_current_pose(self, partial):
        """Start from where the arm IS, not where the operator's hand says.

        Without this the robot lurches the instant it is armed. The wrist
        targets are absolute angles about zero, the elbow comes from a reach
        calibration that knows nothing about the robot's current elbow, and the
        shoulder is the same -- so arming commands a pose unrelated to the one
        the arm is holding, and the onboard dutifully slews through the gap.
        Measured on the first ARMED frame of a real episode: right_elbow
        stepped 53.8 deg, and on a shoulder the same gap swings the whole arm.

        So on the first armed frame, record target-minus-measured per joint and
        subtract it from then on. The first command then equals the pose the
        arm is already in, and everything after is the operator's motion
        relative to it. The offset is dropped when the robot leaves ARMED, so
        each arming re-captures.

        Without measured angles -- an onboard too old to send them -- this does
        nothing rather than guessing, and says so once.
        """
        if not partial:
            return partial
        # getattr throughout: RealG1Sink is built by __new__ in several test
        # paths that never run __init__, and an offset helper must not be the
        # thing that breaks frame validation.
        armed = getattr(self, "onboard_state", None) == "ARMED"
        with self._lock:
            q = getattr(self, "onboard_q", None)
        if not armed:
            self._arm_offset = None
            return partial
        if getattr(self, "_arm_offset", None) is None:
            if not q:
                if not getattr(self, "_warned_no_q", False):
                    self._warned_no_q = True
                    print("[realg1] arming without measured joint angles — "
                          "cannot start from the current pose; expect a step")
                return partial
            self._arm_offset = {
                n: float(v) - float(q[self._ARM_INDEX[n]])
                for n, v in partial.items() if n in self._ARM_INDEX}
            # Versioned so the recorder can write the offset once per capture
            # (the wrist_ref pattern) and a replay can tell one arming from
            # the next. Every command after this is targets-minus-this, and
            # without it written down no file says what the robot received.
            self._arm_offset_v = getattr(self, "_arm_offset_v", 0) + 1
            self._arm_offset_t = time.time()
            worst = max((abs(o) for o in self._arm_offset.values()), default=0.0)
            print(f"[realg1] armed: starting from the current pose "
                  f"(largest offset absorbed {math.degrees(worst):.1f} deg)")
        off = getattr(self, "_arm_offset", None) or {}
        return {n: v - off.get(n, 0.0) for n, v in partial.items()}

    def _record_ik_rejection(self, *, raw, feasible, rejected,
                             source="bridge_post_arm_offset"):
        """Preserve one infeasible post-offset IK target for exact replay."""
        events = getattr(self, "_ik_rejections", None)
        if events is None:
            events = self._ik_rejections = collections.deque(maxlen=256)
        self._ik_rejection_v = getattr(self, "_ik_rejection_v", 0) + 1
        self.n_ik_rejected = getattr(self, "n_ik_rejected", 0) + 1
        event = {
            "schema": "wendy.g1.ik-rejection.v1",
            "v": self._ik_rejection_v,
            "unix_ns": time.time_ns(),
            "source": source,
            "raw_rad": {name: float(value) for name, value in raw.items()},
            "feasible_rad": {name: float(value) for name, value in feasible.items()},
            "rejected": rejected,
        }
        events.append(event)
        return event

    def _project_arm_to_feasible(self, named, *, source="bridge_post_arm_offset"):
        """Project named arm targets into the calibrated physical envelope.

        Retargeter output is already inside the datasheet limits, but the
        arming offset is intentionally applied later and can move the final
        wire command outside this robot's narrower calibrated range.  Clamp at
        this last bridge seam, record the raw request, and still leave the
        onboard clamp in place as independent defence in depth.
        """
        raw = {name: float(value) for name, value in named.items()}
        feasible, rejected = {}, {}
        for name, value in raw.items():
            lo, hi = ROBOT_LIMITS[name]
            applied = min(hi, max(lo, value))
            feasible[name] = applied
            if applied != value:
                rejected[name] = {
                    "requested_rad": value,
                    "feasible_rad": applied,
                    "limit_rad": [float(lo), float(hi)],
                    "reason": "calibrated_joint_limit",
                }
        if rejected:
            self._record_ik_rejection(raw=raw, feasible=feasible,
                                      rejected=rejected, source=source)
        return feasible

    def ik_rejection_events(self):
        """Bounded ordered history; the recorder de-duplicates by version."""
        return list(getattr(self, "_ik_rejections", ()) or ())

    def latest_ik_rejection(self, max_age_s=None):
        events = getattr(self, "_ik_rejections", None)
        if not events:
            return None
        out = dict(events[-1])
        age = max(0.0, (time.time_ns() - out["unix_ns"]) / 1e9)
        if max_age_s is not None and age > float(max_age_s):
            return None
        out["age_s"] = round(age, 4)
        return out

    async def apply(self, targets: dict, absolute: bool = False) -> None:
        """Queue one target dict for the wire.

        `absolute=True` is for a caller that has already
        synchronised its sliders to the measured pose, names exactly the joints
        it means, and does not want the arming offset. Any subset of arm joints
        is legal there -- the whole-set rule below exists to catch a
        retargeter frame that LOST a joint in transit, which cannot happen to
        a frame whose author chose every key.
        """
        # Every joint must be present. Defaulting a missing joint to 0.0 does
        # not mean "leave it alone" -- zero is a specific arm pose, and
        # substituting it silently is how a dropped key becomes a sudden move.
        # A fingers-only frame legitimately carries no arm joints. Requiring
        # them would drop every frame; inventing them would put arm targets on
        # the wire that nobody solved for.
        # A hand that is not in the frame sends no finger targets at all (the
        # retargeter stopped copying the last frame's, which put unmarked stale
        # grips into the recordings). Whole set or nothing here too: all seven
        # absent means the hand is absent and its last grip is held; three of
        # seven absent means a frame lost joints, and is refused below.
        # The waist is its own top-level field on the wire, not a 15th slot
        # in the arm vector. The 14-vector's meaning, the per-side whole-set
        # rule (_ALLOWED) and the onboard's merge_partial_arm all stay exactly
        # as they are; a bridge that sends no waist is indistinguishable from
        # today, and an onboard that does not know the field ignores it.
        # Popped from a copy so the caller's dict -- the one the row records --
        # keeps the joint by name.
        waist = None
        if "waist_yaw_joint" in targets:
            targets = dict(targets)
            waist = targets.pop("waist_yaw_joint")
        hands_present = {s: all(n in targets for n in names)
                         for s, names in (("left", DEX3_LEFT), ("right", DEX3_RIGHT))}
        hands_absent = {s: not any(n in targets for n in names)
                        for s, names in (("left", DEX3_LEFT), ("right", DEX3_RIGHT))}
        want = ([n for n in DEX3_LEFT if not hands_absent["left"]]
                + [n for n in DEX3_RIGHT if not hands_absent["right"]])
        have = [n for n in _ARM if n in targets]
        if hands_absent["left"] and hands_absent["right"] and not have:
            # No hand and no arm: nothing to command. A one-handed frame with
            # no wrist data used to reach here with every joint missing.
            return
        arms_present = len(have) == len(_ARM)
        # Forearm mode drives only the wrists and leaves the shoulder and elbow
        # out, so a partial arm set is legitimate -- but only a whole triple. Anything
        # else is a frame that lost a joint somewhere, and those must still be
        # refused rather than silently having the missing joint held: accepting
        # a partial set in general would turn a corrupted frame into a
        # plausible-looking command.
        partial = {}
        if not arms_present and (absolute or self._is_forearm_set(have)):
            partial = {n: targets[n] for n in have}
            have = []                      # a legitimate partial set
        if arms_present:
            want = _ARM + want
        elif have:
            want = _ARM + want             # incomplete: fail the check below
        missing = [n for n in want if n not in targets]
        if missing:
            print(f"[realg1] dropping frame, missing joints: {missing[:4]}"
                  f"{' …' if len(missing) > 4 else ''}")
            return
        try:
            arm = [float(targets[n]) for n in _ARM] if arms_present else None
            last = getattr(self, "_last_hands", None)
            if last is None:
                measured = getattr(self, "last_hand_q", None)
                if isinstance(measured, dict):
                    left_q, right_q = measured.get("left"), measured.get("right")
                    if (isinstance(left_q, list) and len(left_q) == 7
                            and isinstance(right_q, list) and len(right_q) == 7):
                        last = (list(left_q), list(right_q))
            last = last or ([0.0] * 7, [0.0] * 7)
            left = ([float(targets[n]) for n in DEX3_LEFT] if hands_present["left"]
                    else list(last[0]))            # hold the last grip, or open
            right = ([float(targets[n]) for n in DEX3_RIGHT] if hands_present["right"]
                     else list(last[1]))
        except (TypeError, ValueError) as e:
            print(f"[realg1] dropping unparseable frame: {e}")
            return
        try:
            partial = {n: float(v) for n, v in partial.items()}
            if waist is not None:
                waist = float(waist)
        except (TypeError, ValueError) as e:
            print(f"[realg1] dropping unparseable frame: {e}")
            return
        if not all(math.isfinite(v) for v in (arm or []) + list(partial.values()) + left + right
                   + ([waist] if waist is not None else [])):
            print("[realg1] dropping frame with a non-finite target")
            return
        if not absolute:
            partial = self._offset_to_current_pose(partial)
            if arm is not None:
                # A complete 14-vector from the retargeter (both shoulders with
                # yaw, or the full IK path) is the same kind of thing as a
                # partial one: absolute angles that must start from the pose
                # the arm is holding. Before this only partial frames were
                # offset, so the moment both arms carried all seven joints the
                # arming lurch (53.8 deg on an elbow, measured) came back.
                off = self._offset_to_current_pose(dict(zip(_ARM, arm)))
                arm = [float(off[n]) for n in _ARM]
        if arm is not None:
            feasible = self._project_arm_to_feasible(
                dict(zip(_ARM, arm)),
                source=("manual_absolute" if absolute else "bridge_post_arm_offset"))
            arm = [feasible[name] for name in _ARM]
        elif partial:
            partial = self._project_arm_to_feasible(
                partial,
                source=("manual_absolute" if absolute else "bridge_post_arm_offset"))
        with self._lock:
            self._latest = {"hand_left": left, "hand_right": right}
            self._last_hands = (left, right)
            if arm is not None:
                self._latest["arm"] = arm
            elif partial:
                self._latest["arm_partial"] = {k: float(v) for k, v in partial.items()}
            if waist is not None:
                # No arming offset: the bridge's dial is already seeded from
                # the measured waist on the first ARMED frame, so its target
                # is absolute and starts where the waist is.
                self._latest["waist"] = waist
                # New onboard builds suppress the combined field and consume
                # the independent waist-only stream. Old onboard builds ignore
                # this flag and keep the legacy combined behavior, so bridge
                # can be deployed first without dropping motor-12 control.
                self._latest["waist_detached"] = True
                # Normal XR control has already queued a velocity-only frame.
                # Keep absolute position as a compatibility fallback for
                # callers that do not expose stick velocity.
                if not (isinstance(getattr(self, "_latest_waist", None), dict)
                        and self._latest_waist.get("waist_velocity_only") is True):
                    self._latest_waist = {"waist_only": True, "waist": waist}
                    self._latest_waist_t = time.monotonic()
            self._latest_t = time.monotonic()
            self.n_retargeted += 1

    def fresh_frame(self):
        """The latest targets, or None if they are stale or the dead-man is off."""
        with self._lock:
            if self._latest is None:
                return None
            if time.monotonic() - self._latest_t > self.STALE_S:
                return None
            return self._latest

    def fresh_waist(self):
        """Newest independent waist velocity, bounded by the same liveness age."""
        with self._lock:
            latest = getattr(self, "_latest_waist", None)
            latest_t = getattr(self, "_latest_waist_t", 0.0)
            if latest is None or time.monotonic() - latest_t > self.STALE_S:
                return None
            return dict(latest)

    def set_waist_velocity(self, velocity):
        """Queue the newest stick velocity without integrating position here."""
        velocity = float(velocity)
        if not math.isfinite(velocity):
            return False
        with self._lock:
            self._latest_waist = {
                "waist_velocity_only": True,
                "waist_velocity": velocity,
            }
            self._latest_waist_t = time.monotonic()
        return True

    def commanded(self):
        """-> {joint: radians} as most recently QUEUED FOR THE WIRE, or None.

        Not the retargeter's targets: apply() subtracts the arming offset
        (target-minus-measured on the first ARMED frame, measured at 53.8 deg
        on an elbow) before anything leaves here. Anything comparing a command
        with a measured angle has to use this one, or it sees that offset as a
        permanent error on a joint nothing is touching.
        """
        with self._lock:
            latest = dict(self._latest or {})
        arm = latest.get("arm")
        out = None
        if arm is not None and len(arm) == len(_ARM):
            out = dict(zip(_ARM, arm))
        else:
            partial = latest.get("arm_partial")
            out = dict(partial) if partial else None
        if out is not None and latest.get("waist") is not None:
            out["waist_yaw_joint"] = float(latest["waist"])   # part of what was sent
        return out

    def arm_offset(self):
        """-> {"v": n, "offset": {joint: rad}, "captured_at": unix s} or None.

        The constant the sink subtracts from every arm target while ARMED
        (see _offset_to_current_pose). A row's `targets` minus this is the
        `sent` command. None while not armed or before the first capture;
        the version increments on every capture, so a recorder that writes
        it only when `v` changes writes it once per arming.
        """
        off = getattr(self, "_arm_offset", None)
        if not off:
            return None
        return {"v": getattr(self, "_arm_offset_v", 0),
                "offset": dict(off),
                "captured_at": getattr(self, "_arm_offset_t", None)}

    def last_hands(self):
        """The finger targets most recently queued, or None."""
        with self._lock:
            return self._last_hands

    def measured(self):
        """-> (14 measured radians or None, age in seconds or None)."""
        with self._lock:
            q, t = self.onboard_q, self.onboard_q_t
        return q, (time.monotonic() - t if t else None)

    # ---- control surface ---------------------------------------------------
    COMMANDS = ("take_control", "take_hands", "arm", "hold", "relax", "home", "set_home", "speak",
                "set_home_in_10", "set_home_in_30",
                "calibrate_start", "calibrate_stop", "calibrate_cancel",
                "calibrate_clear", "limp")
    # "status" is internal and deliberately absent: it is polled, not commanded.

    def _debug(self, event, **fields):
        diagnostics = getattr(self, "control_events", None)
        if diagnostics is not None:
            diagnostics.emit(event, **fields)

    def command(self, name: str, payload: dict | None = None) -> bool:
        """Queue one state-machine command. Returns False if it is not one.

        `payload` rides along as extra keys on the wire message
        ({"cmd": name, **payload}). `home` uses it to carry the closed hand
        pose the bridge would command at a full squeeze, so the onboard closes
        the hands the way the grip does rather than to its own stale copy
        (the "not fully closed at home" report). Without a payload the wire
        form is unchanged.
        """
        if name not in self.COMMANDS:
            return False
        if name == "arm":
            self.prepare_hands_for_arm()
        with self._cmd_lock:
            if name == "arm" and not self.connected:
                self._debug("command.not_queued", command=name, reason="robot disconnected")
                return False
            self._cmd_q.append(dict({"cmd": name}, **payload) if payload else name)
        self._debug("command.queued", command=name)
        print(f"[realg1] queued cmd: {name}", flush=True)
        return True

    def prepare_hands_for_arm(self):
        """Seed the command hold from measured fingers before arming."""
        measured = getattr(self, "last_hand_q", None)
        if not isinstance(measured, dict):
            return False
        left, right = measured.get("left"), measured.get("right")
        if not (isinstance(left, list) and len(left) == 7
                and isinstance(right, list) and len(right) == 7):
            return False
        try:
            held = ([float(v) for v in left], [float(v) for v in right])
        except (TypeError, ValueError):
            return False
        if not all(math.isfinite(v) for side in held for v in side):
            return False
        with self._lock:
            self._last_hands = held
        return True

    # Commands that carry a payload. Only these three; the payload is the
    # per-joint gains table and the onboard bounds it itself.
    PAYLOAD_COMMANDS = ("gains", "gains_save", "gains_clear", "motion_limits")

    def command_payload(self, msg: dict) -> bool:
        """Queue a command dict such as {"cmd": "gains", "joints": {...}}."""
        if not isinstance(msg, dict) or msg.get("cmd") not in self.PAYLOAD_COMMANDS:
            return False
        with self._cmd_lock:
            self._cmd_q.append(dict(msg))
        self._debug("command.queued", command=msg.get("cmd"))
        print(f"[realg1] queued cmd: {msg.get('cmd')}", flush=True)
        return True

    def onboard_http_url(self, path="/status"):
        """The onboard's HTTP status URL, derived from the ws URL (no secret)."""
        base = self.url.split("?")[0]
        if base.startswith("ws://"):
            base = "http://" + base[5:]
        elif base.startswith("wss://"):
            base = "https://" + base[6:]
        return base.rsplit("/", 1)[0] + path

    def _stamp(self, frame: dict) -> dict:
        """Sequence number and send time on the wire copy of a frame."""
        self.seq += 1
        return dict(frame, seq=self.seq, t_sent=time.time())

    def _stamp_waist(self, frame: dict) -> dict:
        self.waist_seq = getattr(self, "waist_seq", 0) + 1
        return dict(frame, waist_seq=self.waist_seq, t_sent=time.time())

    def set_locomotion(self, intent):
        """Newest XR intent, consumed once so a stalled bridge cannot renew it."""
        with self._lock:
            self._locomotion_pending = (dict(intent), time.monotonic())

    def pop_locomotion(self):
        with self._lock:
            pending = getattr(self, "_locomotion_pending", None)
            self._locomotion_pending = None
        if pending is None:
            return None
        intent, received = pending
        if time.monotonic() - received > self.STALE_S:
            intent = {"active": False, "vx": 0., "yaw": 0.}
        return {"locomotion_only": True, "locomotion": intent}

    def status(self) -> dict:
        with self._lock:
            age = (time.monotonic() - self._latest_t) if self._latest else None
            motor_temperature = getattr(self, "onboard_motor_temperature", None)
            motor_temperature_t = (getattr(self, "onboard_motor_temperature_t", 0.0)
                                   or 0.0)
        if motor_temperature is not None:
            motor_temperature = dict(
                motor_temperature,
                age_s=round(time.monotonic() - motor_temperature_t, 4))
        return {
            "connected": self.connected,
            "sent": self.n_sent,
            "retargeted": self.n_retargeted,
            "url": self.url.split("?")[0],      # never echo the shared secret
            "state": self.onboard_state,
            "homing": getattr(self, "onboard_homing", False),
            "input_loss_recovery": getattr(self, "input_loss_recovery", None),
            "msg": self.onboard_msg,
            "control_debug": (self.control_events.snapshot() if getattr(self, "control_events", None) else None),
            "control_safety": getattr(self, "onboard_control_safety", None),
            "control_safety_age_s": (round(time.monotonic()-self.onboard_control_safety_t,3)
                if getattr(self, "onboard_control_safety_t", None) is not None else None),
            "hands_fresh": age is not None and age <= self.STALE_S,
            "hands_age_s": round(age, 3) if age is not None else None,
            "acked": self.n_acked,
            "applied": self.n_applied_ack,
            "last_ack": self.last_ack,
            "hand_delivery": {
                "acked": getattr(self, "n_hand_acked", 0),
                "applied": getattr(self, "n_hand_applied_ack", 0),
                "last_ack": getattr(self, "last_hand_ack", None),
                "last_applied": getattr(self, "last_hand_applied", None),
                "last_refused": getattr(self, "last_hand_refused", None),
                "last_superseded": list(
                    getattr(self, "last_hand_superseded", None) or []),
            },
            "q_age_s": (round(time.monotonic() - self.onboard_q_t, 3)
                        if self.onboard_q_t else None),
            "send_hz": getattr(self, "send_hz", None),
            "waist_delivery": {
                "send_hz": getattr(self, "waist_send_hz", None),
                "sent": getattr(self, "n_waist_sent", 0),
                "acked": getattr(self, "n_waist_acked", 0),
                "applied": getattr(self, "n_waist_applied_ack", 0),
                "last_ack": getattr(self, "last_waist_ack", None),
                "last_applied": getattr(self, "last_waist_applied", None),
                "last_refused": getattr(self, "last_waist_refused", None),
                "last_superseded": list(
                    getattr(self, "last_waist_superseded", None) or []),
            },
            "ik_feasibility": {
                "rejected": getattr(self, "n_ik_rejected", 0),
                "latest": self.latest_ik_rejection(),
            },
            # The hottest Dex3 motor and whether any hand reports an error --
            # the two numbers that separate a derating knuckle from a latched
            # one (DEBUG-FINGERS.md). None until an onboard that reads them.
            "hand_temp_max_c": self._hand_temp_max(),
            # Full current vector for diagnostics; the recorder gets the same
            # observation through robot_view().
            "motor_temperature": motor_temperature,
            "body_temp_max_c": ((getattr(self, "onboard_motor_temperature", None) or {})
                                .get("max_c")),
            "body_temp_hottest": list(
                (getattr(self, "onboard_motor_temperature", None) or {})
                .get("hottest") or []),
            # One normalized view across the 29 body motors and both Dex3
            # hands.  The UIs should not have to guess which temperature
            # vector contains the hottest joint.
            "motor_heat_alert": self._motor_heat_alert(),
            "hand_error": self._hand_error(),
            "thermal": self._thermal_monitor().status(),
        }

    def _thermal_monitor(self):
        monitor = getattr(self, "_thermal", None)
        if monitor is None:  # test fixtures and old construction paths bypass __init__
            monitor = self._thermal = ThermalRiseMonitor()
        return monitor

    def _joint_temperatures(self):
        """Newest named motor temperatures from the hands and waist/arms."""
        out = {}
        hs = getattr(self, "last_hand_state", None) or {}
        for side, names in (("left", DEX3_LEFT), ("right", DEX3_RIGHT)):
            state = hs.get(side)
            temps = state.get("temperature") if isinstance(state, dict) else None
            if not isinstance(temps, list):
                continue
            for name, value in zip(names, temps):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    out[name] = value
        full_body = getattr(self, "onboard_motor_temperature", None) or {}
        names = full_body.get("names")
        temps = full_body.get("temperature_c")
        if isinstance(names, list) and isinstance(temps, list):
            for name, value in zip(names, temps):
                if (isinstance(name, str) and isinstance(value, (int, float))
                        and not isinstance(value, bool)):
                    out[name] = value
        body = getattr(self, "onboard_body", None) or {}
        idx = body.get("idx")
        temps = body.get("temperature")
        if isinstance(idx, list) and isinstance(temps, list):
            for motor, value in zip(idx, temps):
                name = _BODY_NAME_BY_INDEX.get(motor)
                if name and isinstance(value, (int, float)) and not isinstance(value, bool):
                    out[name] = value
        return out

    def _observe_thermal(self):
        self._thermal_monitor().observe(self._joint_temperatures())

    def _motor_heat_alert(self):
        """Current hottest named motor and the operator's 80 C alert state."""
        temperatures = {
            name: float(value)
            for name, value in self._joint_temperatures().items()
            if isinstance(name, str) and isinstance(value, (int, float))
            and not isinstance(value, bool) and math.isfinite(float(value))
        }
        if not temperatures:
            return {"threshold_c": MOTOR_HEAT_ALERT_C, "active": False,
                    "joint": None, "temperature_c": None}
        joint, temperature = max(temperatures.items(), key=lambda item: item[1])
        return {"threshold_c": MOTOR_HEAT_ALERT_C,
                "active": temperature >= MOTOR_HEAT_ALERT_C,
                "joint": joint, "temperature_c": temperature}

    def _hand_temp_max(self):
        hs = getattr(self, "last_hand_state", None) or {}
        temps = [t for side in hs.values() if isinstance(side, dict)
                 for t in (side.get("temperature") or []) if isinstance(t, (int, float))]
        return max(temps) if temps else None

    def _hand_error(self):
        """True when any motorstate or hand error word is non-zero; None when unread."""
        hs = getattr(self, "last_hand_state", None) or {}
        seen = False
        for side in hs.values():
            if not isinstance(side, dict):
                continue
            for k in ("motorstate", "error"):
                vals = side.get(k)
                if vals is None:
                    continue
                seen = True
                if any(v for v in vals):
                    return True
        return False if seen else None

    def robot_view(self):
        """The onboard's newest word on itself, for the recorder, per frame.

        state and measured q with how old that measurement is; the newest
        ack's sequence number and applied flag; which joints it clamped to a
        limit and which the leash held. getattr throughout: the sink is built
        by __new__ in several test paths that never run __init__, and the
        recorder's view must not be the thing that breaks a frame.
        """
        with self._lock:
            q = getattr(self, "onboard_q", None)
            q_t = getattr(self, "onboard_q_t", 0.0) or 0.0
            body = getattr(self, "onboard_body", None)
            body_t = getattr(self, "onboard_body_t", 0.0) or 0.0
            motor_temperature = getattr(self, "onboard_motor_temperature", None)
            motor_temperature_t = (getattr(self, "onboard_motor_temperature_t", 0.0)
                                   or 0.0)
        if body is not None:
            # age_s here is since the BRIDGE received it; the onboard's own
            # age (lowstate -> reply) is inside the message and is replaced,
            # because the row is written on this clock and a reader diffing
            # against the command needs the age on the same clock.
            body = dict(body, age_s=round(time.monotonic() - body_t, 4))
        if motor_temperature is not None:
            motor_temperature = dict(
                motor_temperature,
                age_s=round(time.monotonic() - motor_temperature_t, 4))
        return {
            "locomotion": getattr(self, "onboard_locomotion", None),
            "state": getattr(self, "onboard_state", None),
            "msg": getattr(self, "onboard_msg", None) or None,
            "q": q,
            "q_age_s": (round(time.monotonic() - q_t, 4) if q_t else None),
            # Waist + arms q/dq/tau in Unitree motor order (idx), or None
            # when the onboard predates it. What a physics replay diffs against.
            "body": body,
            "motor_temperature": motor_temperature,
            "ack": getattr(self, "last_ack", None),
            "applied": getattr(self, "last_applied", None),
            "clamped": list(getattr(self, "last_clamped", None) or []),
            "leashed": list(getattr(self, "last_leashed", None) or []),
            "refused": getattr(self, "last_refused", None),
            "hand_q": getattr(self, "last_hand_q", None),
            # Dex3 is published independently from arm/waist. These fields
            # preserve its actual completion receipt rather than letting an
            # arm ack imply that the slower hand write already happened.
            "hand_ack": getattr(self, "last_hand_ack", None),
            "hand_applied": getattr(self, "last_hand_applied", None),
            "hand_refused": getattr(self, "last_hand_refused", None),
            "hand_superseded": list(
                getattr(self, "last_hand_superseded", None) or []),
            # The onboard's waist ack: what motor 12 was commanded after its
            # slew and leash, what it measured, and whether it clamped.
            "waist": (dict(getattr(self, "last_waist", None),
                           age_s=round(time.monotonic()
                                       - (getattr(self, "last_waist_t", 0.0) or 0.0), 4))
                      if isinstance(getattr(self, "last_waist", None), dict)
                      and getattr(self, "last_waist_t", 0.0) else None),
            # Everything else the Dex3 reports (temperature, current, mode,
            # error), and which finger joints the onboard backed off as
            # stalled. None from an onboard that predates them.
            "hand_state": getattr(self, "last_hand_state", None),
            "hand_backoff": getattr(self, "last_hand_backoff", None),
            "sent": getattr(self, "n_sent", 0),
        }

    # ---- spotter keyboard control -----------------------------------------
    def _stdin(self):
        mapping = {"take": "take_control", "arm": "arm", "hold": "hold", "stop": "relax"}
        print("[realg1] spotter keys: take | arm | hold | stop")
        for line in sys.stdin:
            c = mapping.get(line.strip().lower())
            if c:
                self.command(c)

    # ---- io loop ----------------------------------------------------------
    def _connection_lost(self):
        diagnostics = getattr(self, "control_events", None)
        if diagnostics:
            diagnostics.disconnected()
            diagnostics.transition("robot_link", False, "robot.link", state="disconnected")
        self.onboard_control_safety = None
        self.onboard_control_safety_t = None
        # An Arm click belongs to the connection on which it was made. Never
        # replay an uncertain/unsent Arm after the robot has returned home.
        with self._cmd_lock:
            self.connected = False
            self._cmd_q = [c for c in self._cmd_q
                           if (c.get("cmd") if isinstance(c, dict) else c) != "arm"]
        self.onboard_state = "?"
        with self._lock:
            self._locomotion_pending = None
        self.onboard_locomotion = None

    def _record(self, stream, payload):
        callback = getattr(self, "record_reading", None)
        if callback is not None:
            try:
                callback(stream, payload)
            except Exception as exc:  # recording must never interrupt control
                print(f"[realg1] recording error: {exc}")

    def _thread(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._run())

    async def _run(self):
        while True:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.ws_connect(self.url) as ws:
                        print(f"[realg1] connected to onboard (STANDBY — 'take' then 'arm')")
                        self.connected = True
                        diagnostics = getattr(self, "control_events", None)
                        if diagnostics:
                            diagnostics.transition("robot_link", True, "robot.link", state="connected")
                        reader = asyncio.create_task(self._read(ws))
                        stale_logged = False
                        now = time.monotonic()
                        next_poll = now
                        next_arm = now
                        next_waist = now
                        next_locomotion = now
                        while not ws.closed:
                            # Poll state rather than inferring it from our own
                            # commands: the onboard can change state on its own
                            # (watchdog freeze), and a page that missed that
                            # would show ARMED for arms that are not.
                            if time.monotonic() >= next_poll:
                                await ws.send_json({"cmd": "status"})
                                next_poll = time.monotonic() + 1.0
                            with self._cmd_lock:
                                cmds, self._cmd_q = self._cmd_q, []
                            try:
                                for i, c in enumerate(cmds):
                                    command = dict(c) if isinstance(c, dict) else {"cmd": c}
                                    diagnostics = getattr(self, "control_events", None)
                                    if diagnostics:
                                        command["request_id"] = diagnostics.sent(command["cmd"], command.get("request_id"))
                                    await ws.send_json(command)
                                    self._debug("command.sent", command=command["cmd"], request_id=command.get("request_id"))
                                    self._record("onboard.tx", command)
                            except Exception:
                                # Preserve unsent commands; disconnect cleanup
                                # discards Arm because its delivery is uncertain.
                                with self._cmd_lock:
                                    self._cmd_q[:0] = cmds[i:]
                                raise
                            now = time.monotonic()
                            frame = self.fresh_frame() if now >= next_arm else None
                            if frame is not None:
                                # Stamped at SEND so the onboard can report how
                                # old a frame is when it finally reads it. Clock
                                # skew makes the absolute value meaningless; the
                                # TREND is what matters -- flat means the read
                                # loop is simply slow, growing means frames are
                                # queuing in the socket.
                                frame = self._stamp(frame)
                                await ws.send_json(frame)
                                self._record("onboard.tx", frame)
                                self.n_sent += 1
                                if diagnostics:
                                    diagnostics.transition("poses", True, "pose.stream", state="fresh")
                                if stale_logged:
                                    print("[realg1] hand data back")
                                stale_logged = False
                            elif now >= next_arm and self._latest is not None and not stale_logged:
                                print("[realg1] hand tracking lost — not sending; "
                                      "the onboard input-loss policy now owns recovery; "
                                      "tracking returning does not re-arm teleop")
                                stale_logged = True
                                if diagnostics:
                                    diagnostics.transition("poses", False, "pose.stream", state="stale")
                            if now >= next_arm:
                                next_arm = _next_deadline(next_arm, self.dt, now)
                            if now >= next_waist:
                                waist = self.fresh_waist()
                                if waist is not None:
                                    waist = self._stamp_waist(waist)
                                    await ws.send_json(waist)
                                    self._record("onboard.tx", waist)
                                    self.n_waist_sent += 1
                                next_waist = _next_deadline(
                                    next_waist, self.waist_dt, now)
                            if now >= next_locomotion:
                                movement = self.pop_locomotion()
                                if movement is not None:
                                    await ws.send_json(movement)
                                    self._record("onboard.tx", movement)
                                next_locomotion = _next_deadline(next_locomotion, .05, now)
                            wake = min(next_arm, next_waist, next_locomotion, next_poll)
                            await asyncio.sleep(max(0.001, wake - time.monotonic()))
            except Exception as e:
                print(f"[realg1] connection lost ({e}); retrying in 2s")
            finally:
                self._connection_lost()
                try:
                    reader.cancel()
                except (NameError, UnboundLocalError):
                    pass
            await asyncio.sleep(2)

    async def _read(self, ws):
        """Track what the onboard commander says its state is."""
        async for msg in ws:
            if msg.type is not aiohttp.WSMsgType.TEXT:
                continue
            self._record("onboard.rx", msg.data)
            try:
                m = json.loads(msg.data)
            except ValueError:
                continue
            prev = self.onboard_state
            diagnostics = getattr(self, "control_events", None)
            if isinstance(m.get("control_safety"), dict):
                self.onboard_control_safety = m["control_safety"]
                self.onboard_control_safety_t = time.monotonic()
                if diagnostics:
                    fields = {k:m["control_safety"].get(k) for k in ("configured","held","fresh","button","release_required")}
                    diagnostics.transition("remote", tuple(fields.values()), "remote.input", **fields)
            if "state" in m and diagnostics:
                diagnostics.transition("robot_state", m["state"], "robot.state", state=m["state"], previous=prev)
            reply = m.get("command_reply")
            if isinstance(reply, dict) and diagnostics:
                diagnostics.reply(reply.get("request_id"), reply.get("command"), reply.get("message"), m.get("state"))
            if "refused" in m and diagnostics:
                diagnostics.transition("frame_refusal", m["refused"], "frame.result", refused=m["refused"], applied=bool(m.get("applied")))
            if isinstance(m.get("locomotion"), dict):
                self.onboard_locomotion = m["locomotion"]
            if isinstance(m.get("arm_reference"), dict):
                self.onboard_arm_reference = m["arm_reference"]
                self.onboard_arm_reference_t = time.monotonic()
            if "state" in m:
                self.onboard_state = m["state"]
            if "homing" in m:
                self.onboard_homing = bool(m["homing"])
            if "input_loss_recovery" in m:
                self.input_loss_recovery = m["input_loss_recovery"]
            if isinstance(m.get("q"), list) and len(m["q"]) == 14:
                # What the arm MEASURED, newest wins. Recorded per frame beside
                # the command, so an episode carries both halves -- during a
                # freeze they diverge entirely and the commanded half is the
                # one that keeps moving. Arrives with every ack and on the
                # onboard's idle stream, so it is frame-rate, not 1 Hz.
                with self._lock:
                    self.onboard_q = m["q"]
                    source_age = m.get("q_age_s")
                    # New onboard includes measurement + outbox residence;
                    # older peers retain arrival-age compatibility. Neither
                    # value measures the unvalidated inter-host transit time.
                    source_age = (float(source_age)
                                  if isinstance(source_age, (int, float))
                                  and math.isfinite(source_age) and source_age >= 0 else 0.0)
                    self.onboard_q_t = time.monotonic() - source_age
            b = m.get("body")
            # Waist + arms with dq and tau, from an onboard new enough to send
            # it. Shape-checked here so a half message can never become a row
            # that a physics replay later trusts; an older onboard sends no
            # `body` at all and the view says None, which is the honest reading.
            if (isinstance(b, dict) and isinstance(b.get("idx"), list)
                    and isinstance(b.get("q"), list) and isinstance(b.get("dq"), list)
                    and len(b["idx"]) == len(b["q"]) == len(b["dq"]) > 0):
                with self._lock:
                    self.onboard_body = b
                    self.onboard_body_t = time.monotonic()
            mt = m.get("motor_temperature")
            if (isinstance(mt, dict) and isinstance(mt.get("idx"), list)
                    and isinstance(mt.get("names"), list)
                    and isinstance(mt.get("temperature_c"), list)
                    and len(mt["idx"]) == len(mt["names"])
                    == len(mt["temperature_c"]) == 29):
                with self._lock:
                    self.onboard_motor_temperature = mt
                    self.onboard_motor_temperature_t = time.monotonic()
            hs = m.get("hand_state")
            if isinstance(hs, dict):
                self.last_hand_state = hs
            if isinstance(hs, dict) or isinstance(b, dict) or isinstance(mt, dict):
                self._observe_thermal()
            superseded_acks = m.get("superseded_acks")
            if isinstance(superseded_acks, list) and superseded_acks:
                # Compact onboard receipt: these targets were read but never
                # applied because a newer target occupied the one-slot mailbox.
                # They deliberately carry no duplicated body telemetry.
                self.n_acked += len(superseded_acks)
                self.last_ack = superseded_acks[-1]
                self.last_applied = False
                self.last_refused = "superseded"
                continue
            if "hand_ack" in m:
                self.n_hand_acked = getattr(self, "n_hand_acked", 0) + 1
                self.last_hand_ack = m["hand_ack"]
                self.last_hand_applied = bool(m.get("applied"))
                self.last_hand_refused = m.get("refused")
                self.last_hand_superseded = list(m.get("superseded") or [])
                if m.get("applied"):
                    self.n_hand_applied_ack = (
                        getattr(self, "n_hand_applied_ack", 0) + 1)
                continue
            if "waist_ack" in m:
                self.n_waist_acked = getattr(self, "n_waist_acked", 0) + 1
                self.last_waist_ack = m["waist_ack"]
                self.last_waist_applied = bool(m.get("applied"))
                self.last_waist_refused = m.get("refused")
                self.last_waist_superseded = list(m.get("superseded") or [])
                waist_ack = m.get("waist")
                if m.get("applied"):
                    self.n_waist_applied_ack = (
                        getattr(self, "n_waist_applied_ack", 0) + 1)
                    if isinstance(waist_ack, dict):
                        self.last_waist = waist_ack
                        self.last_waist_t = time.monotonic()
                continue
            if "ack" in m:
                self.n_acked += 1
                hb = m.get("hand_backoff")
                if isinstance(hb, dict):
                    self.last_hand_backoff = hb
                self.last_ack = m["ack"]
                self.last_applied = bool(m.get("applied"))
                # An older onboard sends neither list; empty is the honest
                # reading of "did not say", and robot_view keeps it a list.
                self.last_clamped = list(m.get("clamped") or [])
                self.last_leashed = list(m.get("leashed") or [])
                waist_ack = m.get("waist")                # None from an older onboard
                # ack_message carries the commander's previous last_waist even
                # when this frame was refused. Only an applied frame proves
                # that command is new enough for bridge anti-windup.
                if (m.get("applied") and m.get("waist_applied", True)
                        and isinstance(waist_ack, dict)):
                    self.last_waist = waist_ack
                    self.last_waist_t = time.monotonic()
                self.last_refused = m.get("refused")     # why, when applied is False
                h = m.get("hands")
                if isinstance(h, dict):
                    self.last_hand_q = h
                if m.get("applied"):
                    self.n_applied_ack += 1
                continue                    # acks carry no message text
            if isinstance(m.get("motion_limits_reply"), dict):
                self.onboard_motion_limits_reply = m["motion_limits_reply"]
            if isinstance(m.get("gains"), dict):
                self.onboard_gains = m["gains"]
            got = m.get("msg") or m.get("error") or ""
            if got:
                # Only overwrite with something. The once-a-second status poll
                # replies with an empty msg, which was erasing refusal text
                # before anyone could read it -- including the one that
                # explained why arming did nothing.
                self.onboard_msg = got
            # Speak only when the state or the message CHANGES. The old rule
            # ("state changed, or any message has ever arrived") printed a line
            # for every idle-stream message once a reply had carried text --
            # twenty a second, burying the Quest-connect and frame-drop lines
            # this log exists to show.
            now = (self.onboard_state, self.onboard_msg)
            if now != getattr(self, "_last_said", None):
                self._last_said = now
                print(f"[realg1] onboard: {self.onboard_state} — {self.onboard_msg}")
