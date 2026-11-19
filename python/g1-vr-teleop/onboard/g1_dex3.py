"""Minimal Dex3-1 hand sender for the G1 (real hardware).

A slimmed-down version of Unitree xr_teleoperate's Dex3_1_Controller — just the
DDS publishers + command-message init + a ctrl_dual_hand() setter. No
multiprocessing, no onboard retargeting: this receives pre-retargeted 7+7 joint
targets and publishes them. Joint order matches retarget_g1.DEX3_LEFT/RIGHT.

Requires unitree_sdk2py on the machine that talks DDS (the robot's onboard
computer). Verified against the API in xr_teleoperate, NOT yet run on hardware.
"""

import logging
import time
from enum import IntEnum

from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import HandCmd_, HandState_
from unitree_sdk2py.idl.default import unitree_hg_msg_dds__HandCmd_

logger = logging.getLogger(__name__)

Dex3_Num_Motors = 7
kTopicDex3LeftCommand = "rt/dex3/left/cmd"
kTopicDex3RightCommand = "rt/dex3/right/cmd"
kTopicDex3LeftState = "rt/dex3/left/state"
kTopicDex3RightState = "rt/dex3/right/state"


class Dex3_1_Left_JointIndex(IntEnum):
    kLeftHandThumb0 = 0
    kLeftHandThumb1 = 1
    kLeftHandThumb2 = 2
    kLeftHandMiddle0 = 3
    kLeftHandMiddle1 = 4
    kLeftHandIndex0 = 5
    kLeftHandIndex1 = 6


class Dex3_1_Right_JointIndex(IntEnum):
    kRightHandThumb0 = 0
    kRightHandThumb1 = 1
    kRightHandThumb2 = 2
    kRightHandIndex0 = 3
    kRightHandIndex1 = 4
    kRightHandMiddle0 = 5
    kRightHandMiddle1 = 6


# The bridge's order -> each hand's hardware slot. Left is the identity; the
# right hand's enum puts INDEX before MIDDLE, which is the whole bug.
_BRIDGE_TO_HW_LEFT = [
    Dex3_1_Left_JointIndex.kLeftHandThumb0, Dex3_1_Left_JointIndex.kLeftHandThumb1,
    Dex3_1_Left_JointIndex.kLeftHandThumb2, Dex3_1_Left_JointIndex.kLeftHandMiddle0,
    Dex3_1_Left_JointIndex.kLeftHandMiddle1, Dex3_1_Left_JointIndex.kLeftHandIndex0,
    Dex3_1_Left_JointIndex.kLeftHandIndex1,
]
_BRIDGE_TO_HW_RIGHT = [
    Dex3_1_Right_JointIndex.kRightHandThumb0, Dex3_1_Right_JointIndex.kRightHandThumb1,
    Dex3_1_Right_JointIndex.kRightHandThumb2, Dex3_1_Right_JointIndex.kRightHandMiddle0,
    Dex3_1_Right_JointIndex.kRightHandMiddle1, Dex3_1_Right_JointIndex.kRightHandIndex0,
    Dex3_1_Right_JointIndex.kRightHandIndex1,
]


# The fully-closed FALLBACK pose, in the bridge's joint order.
#
# grip_q(side, 1.0) from bridge/retarget_g1.py with GRIP_THUMB0 = 0, which
# cannot be imported here -- the bridge does not run on the robot. It lives in
# dex3_stall.py (pure Python) so the onboard can use it without this module's
# DDS imports; re-exported here for the existing callers and test.
#
# It is a FALLBACK: the bridge now sends the pose it would command at a full
# squeeze -- live thumb_0 knob included -- with every `home`, and go_home uses
# that. This copy is what an OLDER bridge gets, and it is the pose the
# operator reported as "not fully closed": thumb_0 neutral, beside the
# fingers instead of across them.
from dex3_stall import CLOSED_LEFT, CLOSED_RIGHT  # noqa: E402,F401


class _RIS_Mode:
    def __init__(self, id=0, status=0x01, timeout=0):
        self.motor_mode = 0
        self.id = id & 0x0F
        self.status = status & 0x07
        self.timeout = timeout & 0x01

    def to_uint8(self):
        m = 0
        m |= (self.id & 0x0F)
        m |= (self.status & 0x07) << 4
        m |= (self.timeout & 0x01) << 7
        return m


class Dex3Sender:
    """Publishes Dex3-1 position commands. kp/kd match Unitree's teleop defaults."""

    # How often the hand state is actually read off DDS. See measured().
    STATE_PERIOD_S = 0.1
    def __init__(self, kp=1.5, kd=0.2, record_reading=None):
        self.record_reading = record_reading
        self._lpub = ChannelPublisher(kTopicDex3LeftCommand, HandCmd_)
        self._lpub.Init()
        self._rpub = ChannelPublisher(kTopicDex3RightCommand, HandCmd_)
        self._rpub.Init()

        self._left = self._init_msg(Dex3_1_Left_JointIndex, kp, kd)
        self._right = self._init_msg(Dex3_1_Right_JointIndex, kp, kd)
        # The RIS mode byte per slot, rewritten every frame in ctrl_dual_hand.
        self._mode = {jid: _RIS_Mode(id=jid, status=0x01).to_uint8() for jid in range(Dex3_Num_Motors)}
        self.kp = kp
        self.kd = kd
        from dex3_thermal import ThermalGuard
        self._thermal = ThermalGuard.from_env()
        self._last_state = {"left": None, "right": None}
        self._last_full = {"left": None, "right": None}
        self._state_t = 0.0
        # Feedback. Never fatal: a hand that will not report is still a hand
        # that grips, and this arrived long after the commands did.
        self._lsub = self._rsub = None
        for name, topic in (("_lsub", kTopicDex3LeftState), ("_rsub", kTopicDex3RightState)):
            try:
                sub = ChannelSubscriber(topic, HandState_)
                sub.Init()
                setattr(self, name, sub)
            except Exception as e:  # noqa: BLE001
                logger.warning("Dex3 state %s unavailable: %s", topic, e)
        logger.info("Dex3Sender ready (kp=%.2f kd=%.2f, state=%s)", kp, kd,
                    bool(self._lsub and self._rsub))

    def _init_msg(self, index_enum, kp, kd):
        msg = unitree_hg_msg_dds__HandCmd_()
        for jid in index_enum:
            msg.motor_cmd[jid].mode = _RIS_Mode(id=jid, status=0x01).to_uint8()
            msg.motor_cmd[jid].q = 0.0
            msg.motor_cmd[jid].dq = 0.0
            msg.motor_cmd[jid].tau = 0.0
            msg.motor_cmd[jid].kp = kp
            msg.motor_cmd[jid].kd = kd
        return msg

    def ctrl_dual_hand(self, left_q7, right_q7):
        """left_q7/right_q7: 7 targets each in the BRIDGE's order.

        The bridge speaks one order for both hands (retarget_g1.DEX3_LEFT and
        DEX3_RIGHT, the latter literally the former with "left" replaced):

            thumb_0, thumb_1, thumb_2, middle_0, middle_1, index_0, index_1

        The hardware does not. Dex3_1_Left_JointIndex agrees; Dex3_1_Right_-
        JointIndex has INDEX before MIDDLE. This used to copy slot for slot,
        so on the right hand every middle-finger command drove the index
        finger and every index command drove the middle.

        Invisible with a trigger grip, because grip_q moves all seven together
        and swapping two of them changes nothing anyone can see. It shows on
        hand tracking, where the fingers move independently -- and the
        operator's report of the right hand doing "very small finger
        movement" while on hand tracking is consistent with it.
        
        NOT yet confirmed on hardware; the enums are unambiguous but the
        physical check is one finger at a time with the operator watching.
        """
        # mode/kp/kd EVERY frame, not only at construction.
        #
        # relax() zeroes them -- and hold() calls relax(), so every hold, every
        # go_home (which holds first), and every watchdog freeze left the
        # fingers at zero stiffness. Nothing ever put it back: this method only
        # wrote `q`. So after the first hold the hands stopped responding for
        # the rest of the session however many times the operator re-armed,
        # and the robot sat with a command of "open" and a hand measured shut.
        #
        # The operator found it as "when i press home and then arm the hands
        # are not responding", which is exactly right and exactly this.
        #
        # A motor that meets resistance can also latch its enable state. The
        # knuckles carry the whole finger and are the first ones to do that;
        # writing mode again gives the Dex3 the same explicit recovery path as
        # writing the gains again.  This costs 14 small assignments and makes
        # the message say what it means on its own, rather than depending on
        # no one having changed a command field since startup.
        # Per-joint gain scale from the hand's own temperature. A knuckle
        # approaching its cutout grips with LESS FORCE rather than reaching
        # 90 C and being disabled for the rest of the session. A trip cannot
        # be undone in software -- the protocol's only enable is the status
        # bits we already write at 100 Hz, and a tripped motor ignores them --
        # so the only move available is not to trip.
        #
        # Position is untouched: the finger still closes where it was told,
        # with less holding torque, and holding torque is what makes the heat.
        # Moving the finger somewhere else would be a grip nobody asked for.
        ls = self._thermal.scale("left",
                                 (self._last_full.get("left") or {}).get("temperature"))
        rs = self._thermal.scale("right",
                                 (self._last_full.get("right") or {}).get("temperature"))
        for i, jid in enumerate(_BRIDGE_TO_HW_LEFT):
            self._left.motor_cmd[jid].mode = self._mode[jid]
            self._left.motor_cmd[jid].q = float(left_q7[i])
            self._left.motor_cmd[jid].kp = self.kp * ls[i]
            self._left.motor_cmd[jid].kd = self.kd * ls[i]
        for i, jid in enumerate(_BRIDGE_TO_HW_RIGHT):
            self._right.motor_cmd[jid].mode = self._mode[jid]
            self._right.motor_cmd[jid].q = float(right_q7[i])
            self._right.motor_cmd[jid].kp = self.kp * rs[i]
            self._right.motor_cmd[jid].kd = self.kd * rs[i]
        left_result = self._lpub.Write(self._left)
        right_result = self._rpub.Write(self._right)
        recorder = getattr(self, "record_reading", None)
        if recorder is not None:
            recorder("rt/dex3/left/cmd.write", {"message": self._left,
                                                "write_result": left_result})
            recorder("rt/dex3/right/cmd.write", {"message": self._right,
                                                 "write_result": right_result})

    def measured(self):
        """-> {"left": [7] or None, "right": [7] or None}, in the BRIDGE's order.

        The state topics were imported here from the start and never
        subscribed, so the hands have been open loop: no way to tell a finger
        blocked by an object from one closing freely, no confirmation that a
        grasp happened, and every recorded episode carrying commanded finger
        positions with no measurement beside them. The arms have reported
        measured angles at frame rate since the per-frame acks landed; the
        hands reported nothing.

        With this, a finger commanded closed that stops short is holding
        something, and how far short it stopped times kp is roughly how hard.
        """
        # RATE LIMITED, and that is the whole point of this block.
        #
        # This is called from ack_message, which runs on EVERY frame -- 90/s.
        # Reading two DDS topics that often put blocking reads back on the hot
        # path and measurably cost it: arm_cmd_ms went 0.27 -> 6.8 and the
        # liveness watchdog started firing again, which is the same starvation
        # the 250 Hz publisher loop had just been fixed for. Grasp detection
        # needs about 10 Hz; the arm's own measured angles still ride every
        # frame. Between reads the last sample is returned, which is what a
        # 10 Hz sensor means anyway.
        self._poll()
        return {side: (self._last_state.get(side)) for side in ("left", "right")}

    def measured_state(self):
        """-> per side, everything the hand reports, in the BRIDGE's order, or None.

            {"q", "dq", "tau_est", "temperature", "mode", "motorstate": [7] each,
             "power_v", "power_a", "error": [2]}

        Position alone could not say WHY the knuckles stopped following
        (DEBUG-FINGERS.md): a knuckle at 15 % of its command looks the same
        whether it is hot, latched, or simply blocked. HandState_ carries
        per-motor temperature (two sensors; the hotter is kept), estimated
        torque, the mode byte and an error word, plus current and an error
        pair per hand. A rising temperature during grips says thermal
        derating; a non-zero motorstate says latched. Each field is None when
        the message does not carry it -- never zero, which would read as
        "cold and healthy".

        Same 10 Hz read as measured(); this is the same sample, more of it.
        """
        self._poll()
        return {side: (dict(self._last_full[side]) if self._last_full.get(side) else None)
                for side in ("left", "right")}

    @staticmethod
    def _field(ms, order, attr, conv=float):
        try:
            vals = [getattr(ms[jid], attr) for jid in order]
        except (AttributeError, IndexError, TypeError):
            return None
        try:
            return [conv(v) for v in vals]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _hotter(temps):
        """int16[2] per motor -> the higher sensor, or None when unreadable."""
        if temps is None:
            return None
        out = []
        for t in temps:
            try:
                out.append(int(max(t)) if isinstance(t, (list, tuple)) and len(t) else int(t))
            except (TypeError, ValueError):
                return None
        return out

    def _poll(self):
        """Read both state topics if the rate limit allows; update the samples."""
        now = time.monotonic()
        if now - self._state_t < self.STATE_PERIOD_S:
            return
        self._state_t = now
        for side, sub, order in (("left", self._lsub, _BRIDGE_TO_HW_LEFT),
                                 ("right", self._rsub, _BRIDGE_TO_HW_RIGHT)):
            try:
                msg = sub.Read() if sub is not None else None
                if msg is None:
                    continue
                recorder = getattr(self, "record_reading", None)
                if recorder is not None:
                    recorder(f"rt/dex3/{side}/state", msg)
                ms = msg.motor_state
                q = [float(ms[jid].q) for jid in order]
            except Exception:  # noqa: BLE001 -- feedback must never cost a frame
                continue
            self._last_state[side] = q
            full = {"q": q,
                    "dq": self._field(ms, order, "dq"),
                    "tau_est": self._field(ms, order, "tau_est"),
                    "temperature": self._hotter(self._field(ms, order, "temperature", conv=lambda v: v)),
                    "mode": self._field(ms, order, "mode", conv=int),
                    "motorstate": self._field(ms, order, "motorstate", conv=int)}
            for name in ("power_v", "power_a"):
                v = getattr(msg, name, None)
                try:
                    full[name] = float(v) if v is not None else None
                except (TypeError, ValueError):
                    full[name] = None
            err = getattr(msg, "error", None)
            try:
                full["error"] = [int(x) for x in err] if err is not None else None
            except (TypeError, ValueError):
                full["error"] = None
            self._last_full[side] = full

    def relax(self):
        """Zero-stiffness both hands (kp=kd=0) — used on shutdown / e-stop."""
        for msg in (self._left, self._right):
            for jid in range(Dex3_Num_Motors):
                msg.motor_cmd[jid].kp = 0.0
                msg.motor_cmd[jid].kd = 0.0
        left_result = self._lpub.Write(self._left)
        right_result = self._rpub.Write(self._right)
        recorder = getattr(self, "record_reading", None)
        if recorder is not None:
            recorder("rt/dex3/left/cmd.write", {"message": self._left,
                                                "write_result": left_result})
            recorder("rt/dex3/right/cmd.write", {"message": self._right,
                                                 "write_result": right_result})
