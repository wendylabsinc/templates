"""G1 retargeting adapter.

Turns one Quest frame (per-hand 25 wrist-local landmarks + wrist pose in head
frame) into a {joint_name: target_rad} dict for the full G1 upper body:

    - fingers  -> dex-retargeting (DexPilot) using Unitree's unitree_dex3.yml
    - arms      -> Pinocchio/CasADi QP IK (G1_29_ArmIK) from wrist SE3 targets

Heavy deps (dex_retargeting, pinocchio, casadi) are imported at construction,
not import time, so the bridge still boots without them for the log-sink data
path test. Build one G1Retargeter and call .process(frame) per frame.

Hand/finger logic mirrors Unitree's robot_hand_unitree.py; arm logic mirrors
their teleop_hand_and_arm.py solve_ik call. Both from xr_teleoperate (Apache-2.0).
"""

import json
import math as _math
import os
import threading
import time

import numpy as np
import yaml

_ASSETS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets"))
_DEX3_YML = os.path.join(_ASSETS, "unitree_hand", "unitree_dex3.yml")

# Dex3-1 hardware joint order (7/hand) — from Unitree hand_retargeting.py.
DEX3_LEFT = ["left_hand_thumb_0_joint", "left_hand_thumb_1_joint", "left_hand_thumb_2_joint",
             "left_hand_middle_0_joint", "left_hand_middle_1_joint",
             "left_hand_index_0_joint", "left_hand_index_1_joint"]
DEX3_RIGHT = [n.replace("left", "right") for n in DEX3_LEFT]

# Arm joint output order (7/arm) — reduced-robot order in the G1 URDF: left arm
# then right arm. solve_ik returns q in exactly this order.
ARM_LEFT = ["left_shoulder_pitch_joint", "left_shoulder_roll_joint", "left_shoulder_yaw_joint",
            "left_elbow_joint", "left_wrist_roll_joint", "left_wrist_pitch_joint", "left_wrist_yaw_joint"]
ARM_RIGHT = [n.replace("left", "right") for n in ARM_LEFT]

# ---------------------------------------------------------------------------
# Coordinate remap: WebXR head frame (X right, Y up, -Z forward) -> G1 torso
# frame (X forward, Y left, Z up). CALIBRATE against the sim before trusting arm
# poses — this rotation + the head->shoulder offset are the two things most
# likely to need a tweak once you watch it in the viewer.
# ---------------------------------------------------------------------------
_R_XR_TO_ROBOT = np.array([
    [0.0, 0.0, -1.0],   # robot X (forward) = -Z_xr (forward)
    [-1.0, 0.0, 0.0],   # robot Y (left)    = -X_xr (right -> left)
    [0.0, 1.0, 0.0],    # robot Z (up)      =  Y_xr (up)
])
# Operator head -> robot PELVIS offset, in the robot frame (m).
#
# The IK solves in the pelvis frame: _vendor_robot_arm_ik locks the waist
# joints, so the reduced model's root stays the URDF root. This offset must
# therefore be the head's position in the pelvis frame -- UPWARD.
#
# It was -0.35. The old name (_HEAD_TO_TORSO) encoded the error: that is the
# head->pelvis vector, where the formula needs pelvis->head. Net error 0.84 m
# downward, which put every target BELOW THE ROBOT'S FEET. Measured against the
# URDF, no hand pose an operator can adopt landed inside the workspace except
# hands held above the head. The arms strained at the joint-limit box and crept
# as the unreachable direction changed -- indistinguishable, from outside, from
# a dead link. It is why teleop "did not follow my hands".
#
# DERIVED, not estimated: this is the G1's own head camera (`d435_link`) in the
# pelvis frame, computed from assets/g1/g1_body29_hand14.urdf. The operator's
# headset sits at their eyes, so the robot's eyes are the right analogue.
#
#   d435_link  (+0.054, +0.018, +0.474)   <- the robot's eyes
#   mid360     (-0.004,  0.000, +0.460)
#   shoulders  ( 0.000, +-0.100, +0.292)
#
# The earlier +0.49 was a guess from the shoulder height and happened to land
# within 1.6 cm, but it carried NO forward term. Upstream xr_teleoperate uses
# x += 0.15; that is their robot's geometry, not ours — measured here it is
# +0.054, so copying 0.15 would push every target 10 cm too far forward.
#
# y is deliberately 0 rather than the camera's +0.018: that offset is where the
# RealSense happens to be bolted, and carrying it would make the operator's
# left and right workspaces asymmetric, which matters more than 18 mm.
_PELVIS_TO_HEAD = np.array([0.054, 0.0, 0.474])

# Human reach maps onto a SHORTER arm, so this scales DOWN.
# Measured from the URDF by sampling the 7-DoF chain inside its joint limits:
# max reach from the shoulder is 0.459 m. A human's is about 0.62 m.
# 0.459 / 0.62 = 0.74. It was 1.1, which is the wrong direction as well as the
# wrong size.
_REACH_SCALE = 0.74

# Measured workspace of the L_ee/R_ee frames in the pelvis frame, for the
# self-test to assert against. A retargeter that aims outside this cannot be
# followed, however healthy every other part of the chain looks.
_WORKSPACE = {"x": (-0.43, 0.43), "y": (-0.28, 0.55), "z": (-0.12, 0.73)}
_MAX_REACH_M = 0.459
_SHOULDER_L = np.array([0.0, 0.100, 0.292])
_SHOULDER_R = np.array([0.0, -0.100, 0.292])


def _urdf_joint_rpy(path, names):
    """-> {joint name: its fixed rpy rotation}, read once at import.

    The wrist mapping needs the orientation of the robot's FOREARM under the
    shoulder and elbow angles it just commanded, and that is forward kinematics
    -- which means every joint's rpy, not just its axis. Summing offsets while
    ignoring rpy is the specific mistake that produced two wrong finger
    rotations in this repo.
    """
    import xml.etree.ElementTree as ET
    out = {}
    for j in ET.parse(path).getroot().findall("joint"):
        if j.get("name") not in names:
            continue
        o = j.find("origin")
        r, p_, y = [float(v) for v in ((o.get("rpy", "0 0 0") if o is not None
                                        else "0 0 0").split())]
        cr, sr, cp, sp, cy, sy = (_math.cos(r), _math.sin(r),
                                  _math.cos(p_), _math.sin(p_),
                                  _math.cos(y), _math.sin(y))
        out[j.get("name")] = (
            np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]])
            @ np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]])
            @ np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]]))
    return out


_G1_URDF = os.path.join(_ASSETS, "g1", "g1_body29_hand14.urdf")

_ARM_CHAIN = [f"{s}_{j}_joint" for s in ("left", "right")
              for j in ("shoulder_pitch", "shoulder_roll", "shoulder_yaw",
                        "elbow", "wrist_roll")]
_JOINT_RPY = _urdf_joint_rpy(_G1_URDF, set(_ARM_CHAIN))


def _rx(a):
    c, s_ = _math.cos(a), _math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s_], [0, s_, c]])


def _ry(a):
    c, s_ = _math.cos(a), _math.sin(a)
    return np.array([[c, 0, s_], [0, 1, 0], [-s_, 0, c]])


def _rz(a):
    c, s_ = _math.cos(a), _math.sin(a)
    return np.array([[c, -s_, 0], [s_, c, 0], [0, 0, 1]])


def _urdf_joint_xyz(path, names):
    """-> {joint name: its fixed translation}."""
    import xml.etree.ElementTree as ET
    out = {}
    for j in ET.parse(path).getroot().findall("joint"):
        if j.get("name") not in names:
            continue
        o = j.find("origin")
        out[j.get("name")] = np.array(
            [float(v) for v in ((o.get("xyz", "0 0 0") if o is not None
                                 else "0 0 0").split())])
    return out


_JOINT_XYZ = _urdf_joint_xyz(_G1_URDF, set(_ARM_CHAIN))


def shoulder_to_wrist(side, elbow, yaw=0.0):
    """Shoulder-roll origin -> WRIST, in the roll joint's frame, for this elbow.

    This is the whole two-link fix, and it is the same algebra the code already
    had with one constant changed.

    Writing the hand position out, everything distal to the roll joint collapses
    into a single vector once the elbow angle is known:

        p_hand = Rp @ Ry(pitch) @ Rr @ Rx(roll) @ w(elbow)

    so pitch and roll are solved exactly as before -- only against `w`, the
    vector to the HAND, instead of against the vector to the ELBOW. Using the
    elbow one means the shoulder aims the upper arm at the operator's hand,
    which is correct only with a straight elbow; the elbow then bends away and
    the hand lands elsewhere. Measured at a constant 20.2 deg before this.
    """
    J, X = _JOINT_RPY, _JOINT_XYZ
    forearm = (X[f"{side}_elbow_joint"]
               + J[f"{side}_elbow_joint"] @ _ry(elbow)
               @ X[f"{side}_wrist_roll_joint"])
    return (J[f"{side}_shoulder_roll_joint"]
            @ (X[f"{side}_shoulder_yaw_joint"]
               + J[f"{side}_shoulder_yaw_joint"] @ _rz(yaw) @ forearm))


def forearm_orientation(side, pitch, roll, elbow, yaw=0.0):
    """Orientation of the FOREARM in the torso frame, by forward kinematics.

    This is the frame the three wrist joints act in. Holding the shoulder made
    it constant, which is why a fixed rotation worked at all; driving the
    shoulder makes it move, and a wrist mapping that ignores it is wrong by the
    whole shoulder motion -- up to 108 deg of roll on a recorded session. The
    operator sees the hand tilt whenever they raise the arm, and the natural
    suspect is the wrist mapping, which is verified to +0.99 and not at fault.
    """
    J = _JOINT_RPY
    return (J[f"{side}_shoulder_pitch_joint"] @ _ry(pitch)
            @ J[f"{side}_shoulder_roll_joint"] @ _rx(roll)
            @ J[f"{side}_shoulder_yaw_joint"] @ _rz(yaw)
            @ J[f"{side}_elbow_joint"] @ _ry(elbow)
            @ J[f"{side}_wrist_roll_joint"])


def _so3(M):
    """Nearest true rotation to `M`.

    The wrist matrices are written out to four decimals, so as literals they
    are very slightly not orthonormal -- enough that conjugating by them leaves
    ~5e-5 rad on a hand that has not moved. Small, but it is drift with no
    physical cause, and it makes "did not move" untestable.
    """
    u, _, vt = np.linalg.svd(np.asarray(M, float))
    R = u @ vt
    if np.linalg.det(R) < 0:
        u[:, -1] *= -1
        R = u @ vt
    return R


def _quat_to_R(q):
    """[x,y,z,w] -> 3x3 rotation matrix.

    Normalises first. Quaternions arrive over the wire rounded to four
    decimals (`toFixed(4)` in web/teleop.js), so they are never exactly unit
    and the matrix built from them is never exactly a rotation. Small, but it
    is the kind of small that shows up as a hand that will not hold still.
    A degenerate quaternion becomes identity rather than NaN -- a dropped pose
    should read as "no rotation", not poison every joint downstream.
    """
    q = np.asarray(q, float)
    n = float(np.linalg.norm(q))
    if not np.isfinite(n) or n < 1e-9:
        return np.eye(3)
    x, y, z, w = q / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w),     2 * (x * z + y * w)],
        [2 * (x * y + z * w),     1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w),     2 * (y * z + x * w),     1 - 2 * (x * x + y * y)],
    ])


def _known_keys(cfg: dict) -> dict:
    """Drop config keys the installed dex-retargeting does not accept.

    Unitree's `unitree_dex3.yml` is written for their own fork and carries
    `target_link_human_indices_dexpilot`, which no released version of
    dex-retargeting accepts (checked against 0.4.5, 0.4.6 and 0.5.0) -- the
    released field is `target_link_human_indices`. Passing it through raises
    TypeError at construction.

    Dropping it rather than renaming it is deliberate: the two are not
    obviously the same thing, and Unitree's own comment beside it says "if you
    do not know exactly how it is used, please leave it to None for default".
    So we take the default and say so, instead of silently applying a fork's
    tuning array to a field that may mean something else.

    CONSEQUENCE: finger mapping uses the library's default correspondence, not
    Unitree's tuned one. Compare against their stack before trusting fine
    finger placement.
    """
    import dataclasses

    from dex_retargeting.retargeting_config import RetargetingConfig

    allowed = {f.name for f in dataclasses.fields(RetargetingConfig)}

    # Unitree's key is a pure RENAME of the released one, same 2xN shape, so map
    # it through rather than dropping it.
    #
    # Dropping it left target_link_human_indices as None, and the library then
    # computes np.stack([origin_link_index, task_link_index]) * 4 -- multiples of
    # four, so it reads WebXR joints 4, 8, 12: thumb tip, index DISTAL, middle
    # INTERMEDIATE. Unitree's [4, 9, 14] are the actual tips. We were driving
    # DexPilot off knuckles. Fingers still curl when you curl yours, which is
    # why every finger check passed.
    cfg = dict(cfg)
    rename = cfg.pop("target_link_human_indices_dexpilot", None)
    if rename is not None and "target_link_human_indices" not in cfg:
        cfg["target_link_human_indices"] = rename
        print("[retarget] mapped target_link_human_indices_dexpilot -> "
              "target_link_human_indices (fingertips 4/9/14, not 4/8/12)")

    dropped = sorted(set(cfg) - allowed)
    if dropped:
        print(f"[retarget] ignoring config keys this dex-retargeting does not know: {dropped}")
    return {k: v for k, v in cfg.items() if k in allowed}


# WebXR wrist frame -> Dex3 base_link frame, per hand.
#
# The landmarks handFeatures produces are WRIST-LOCAL in the WebXR frame, where
# (W3C Hand Input Module) -Z runs along the bone away from the wrist and -Y
# points out of the palm. dex_retargeting compares them against robot links in
# the Dex3 base_link frame. Measured from the vendored URDFs:
#
#   LEFT   fingers +X   thumb -Y   index-middle +Z
#   RIGHT  fingers +X   thumb +Y   index-middle +Z    (mirrored only in Y)
#
# So the fingers run along +X for the robot and -Z for the headset -- about 90
# degrees apart -- and NOTHING rotated between them. The arm path has exactly
# such a matrix (_R_XR_TO_ROBOT); the finger path never had one. That is the
# leading explanation for fingers that saturate at their limits under both
# DexPilot and vector retargeting while the input landmarks are provably
# perfect (0 fabricated of 25 per hand at 90 fps).
#
# Derived by requiring the two well-determined axes to land correctly (fingers
# -> +X, index-middle -> +Z) AND the result to be a proper rotation rather than
# a reflection, which fixes the remaining sign.
#
# CAVEAT, stated because it has not been measured: the THUMB axis does not
# follow from this. The Dex3 thumb sits at -+Y in its rest pose, which is its
# own mount angle rather than where a human thumb sits relative to the palm.
# If the fingers track and the thumb does not, that is this, not the rotation.
# MEASURED, not derived, and the derivation that came first was wrong twice.
#
# Attempt 1 read the W3C text (-Z along the bone, -Y out of the palm) and scored
# 6.3 % / 0.8 % mean joint range on real data -- essentially dead.
#
# Attempt 2 "measured" the URDF by summing joint xyz offsets while IGNORING
# their rpy, which reported the fingers along +X. With the rotations applied the
# standalone hand URDF that dex_retargeting actually loads puts them along -Y:
#
#   index_tip  ignoring rpy (0.174, 0.002, 0.028)   WITH rpy (0.002, -0.173, 0.028)
#
# Note dex_retargeting loads assets/unitree_hand/unitree_dex3_{left,right}.urdf,
# NOT the hand as mounted in the whole-body model -- different frames, and
# measuring the wrong one is how a test comes to agree with a mistake.
#
# What settled it was empirical: replaying a recorded 22 s open/close session
# (1984 frames, both hands, 0 fabricated landmarks) through all 24 proper
# axis-permutation rotations, scoring each on joint range used AND direction:
#
#   this pair      left 59.1 %  right 59.7 %   every joint correct direction
#   same-both      left 44.2 %  right 47.5 %   thumb joints INVERTED
#   spec-derived   left  6.3 %  right  0.8 %   dead
#
# On the robot it measured 68.6 % / 62.6 % mean, with index_1 and middle_1 at
# 100 %, measured offline against a recorded session.
_FINGER_ROT = {
    "left":  np.array([[-1, 0, 0], [0, 0, 1], [0,  1, 0]], dtype=float),
    "right": np.array([[ 1, 0, 0], [0, 0, 1], [0, -1, 0]], dtype=float),
}


def _finger_rot(side):
    """Override with TELEOP_FINGER_ROT=none, or nine comma-separated numbers."""
    env = os.environ.get("TELEOP_FINGER_ROT", "").strip()
    if not env:
        return _FINGER_ROT[side]
    if env.lower() in ("none", "off", "identity"):
        return np.eye(3)
    vals = [float(x) for x in env.replace(" ", "").split(",")]
    if len(vals) != 9:
        raise ValueError("TELEOP_FINGER_ROT needs 9 numbers, row-major")
    R = np.array(vals, dtype=float).reshape(3, 3)
    # Applied to BOTH hands verbatim. The derived pair mirrors the right hand
    # because the index-middle axis mirrors in the WebXR wrist frame while the
    # Dex3 keeps it on +Z for both -- but on hardware that mirror killed the
    # right hand outright while the left came alive, so the override exists to
    # test the un-mirrored alternative without a code change.
    if os.environ.get("TELEOP_FINGER_ROT_MIRROR", "1") not in ("0", "no", "off"):
        if side == "right":
            R = np.diag([1.0, -1.0, -1.0]) @ R
    return R


# DexPilot's scaling_factor, which trades KNUCKLE travel against FINGERTIP
# travel. Unitree's config ships 1.0 with a comment claiming the field applies
# to vector retargeting only -- it does not: DexPilotOptimizer does
# `normal_vec = target_vector * self.scaling`.
#
# Swept offline against tests/fixtures/hand_open_close.json, % of each joint's
# range used (left hand; right within 3 points):
#
#   scale   index_0  index_1  middle_0  middle_1
#    0.60     100 %      0 %     100 %      0 %    all knuckle, no fingertip
#    0.75      88 %     50 %     100 %    100 %
#    0.85      74 %     66 %     100 %    100 %   <- best worst-joint
#    1.00      50 %     88 %      81 %    100 %    knuckles half-used
#    1.20      20 %     99 %      50 %    100 %
#
# At 1.0 the operator reported exactly what the table predicts: fingertips
# tracking well, knuckles not closing into a fist. 0.85 maximises the WORST
# joint rather than the mean, because a finger is only as useful as its stiffest
# joint, measured offline against a recorded session.
FINGER_SCALE = 0.85

# Arm joint limits, from assets/g1/g1_body29_hand14.urdf. Only the forearm
# joints are listed; forearm mode never commands the shoulder.
ARM_JOINT_LIMITS = {
    # Shoulder, straight from the URDF (the datasheet range). Yaw is here
    # because _shoulder_q now solves for it -- only when pitch and roll alone
    # cannot reach inside the ROBOT's limits, see load_robot_limits below.
    "left_shoulder_pitch_joint": (-3.0892, 2.6704),
    "left_shoulder_roll_joint": (-1.5882, 2.2515),
    "left_shoulder_yaw_joint": (-2.618, 2.618),
    "right_shoulder_pitch_joint": (-3.0892, 2.6704),
    "right_shoulder_roll_joint": (-2.2515, 1.5882),
    "right_shoulder_yaw_joint": (-2.618, 2.618),
    "left_elbow_joint": (-1.0472, 2.0944),
    "left_wrist_roll_joint": (-1.9722, 1.9722),
    "left_wrist_pitch_joint": (-1.6144, 1.6144),
    "left_wrist_yaw_joint": (-1.6144, 1.6144),
    "right_elbow_joint": (-1.0472, 2.0944),
    "right_wrist_roll_joint": (-1.9722, 1.9722),
    "right_wrist_pitch_joint": (-1.6144, 1.6144),
    "right_wrist_yaw_joint": (-1.6144, 1.6144),
}

# The robot's own limits, walked by hand and saved by the onboard
# (real/calibration/<device>.json), narrowed onto the datasheet above.
#
# The bridge needs them for one decision: whether a shoulder solution is
# reachable. On the first full-stack episode the right hand worked across the
# body and the solve, scoring against the DATASHEET (+91 deg of inward roll),
# called +77 deg reachable. This robot's right shoulder roll is calibrated to
# +5 deg inward -- past that the upper arm meets the torso -- so 58 % of the
# right shoulder commands went to a joint that could not follow, the arm sat on
# its limit and the leash dragged every right joint behind it. The onboard
# still clamps; this only lets the solve know what it is clamping to.
#
# Datasheet when the file is missing or malformed, and never wider than the
# datasheet whatever the file says.
_ARM14 = ARM_LEFT + ARM_RIGHT
_ROBOT_CAL_FILE = os.environ.get(
    "TELEOP_ARM_CAL",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "real",
                 "calibration", "unitree-g1-nx-2.json"))


def load_robot_limits(path=_ROBOT_CAL_FILE):
    """-> ({joint: (lo, hi)}, source path or None)."""
    out = dict(ARM_JOINT_LIMITS)
    try:
        with open(path) as fh:
            lim = json.load(fh)["limits"]
        if len(lim) != len(_ARM14):
            raise ValueError("wrong length")
        for name, (lo, hi) in zip(_ARM14, lim):
            dlo, dhi = ARM_JOINT_LIMITS[name]
            out[name] = (max(dlo, float(lo)), min(dhi, float(hi)))
    except (OSError, ValueError, KeyError, TypeError):
        return dict(ARM_JOINT_LIMITS), None
    return out, path


ROBOT_LIMITS, ROBOT_LIMITS_SOURCE = load_robot_limits()


def _wrap(a):
    """-> a in (-pi, pi]."""
    return (a + _math.pi) % (2 * _math.pi) - _math.pi


DEX3_RANGE = {
    "left_hand_thumb_0_joint": (-1.04719755, 1.04719755),
    "left_hand_thumb_1_joint": (-0.72431163, 0.920),
    "left_hand_thumb_2_joint": (0.0, 1.74532925),
    "left_hand_middle_0_joint": (-1.57079632, 0.0),
    "left_hand_middle_1_joint": (-1.74532925, 0.0),
    "left_hand_index_0_joint": (-1.57079632, 0.0),
    "left_hand_index_1_joint": (-1.74532925, 0.0),
    "right_hand_thumb_0_joint": (-1.04719755, 1.04719755),
    "right_hand_thumb_1_joint": (-0.920, 0.72431163),
    "right_hand_thumb_2_joint": (-1.74532925, 0.0),
    "right_hand_middle_0_joint": (0.0, 1.57079632),
    "right_hand_middle_1_joint": (0.0, 1.74532925),
    "right_hand_index_0_joint": (0.0, 1.57079632),
    "right_hand_index_1_joint": (0.0, 1.74532925),
}


class TriggerGripSlew:
    """Independent, time-based trigger filtering for the two Dex3 hands.

    A full trigger stroke used to become a full hand stroke in one headset
    frame.  The hands now move at a bounded fraction of their range per
    second, and a tracking gap is capped so reconnecting cannot cause a snap.
    A non-positive rate is an explicit test/calibration bypass.
    """

    def __init__(self, rate_per_s=2.0, max_dt_s=0.1):
        self.rate_per_s = float(rate_per_s)
        self.max_dt_s = float(max_dt_s)
        if not _math.isfinite(self.rate_per_s) or self.rate_per_s < 0:
            raise ValueError("trigger grip rate must be finite and >= 0")
        if not _math.isfinite(self.max_dt_s) or self.max_dt_s <= 0:
            raise ValueError("trigger grip max dt must be finite and > 0")
        self._value = {"left": 0.0, "right": 0.0}
        self._time = {}
        self._pending = {}

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        return cls(rate_per_s=float(env.get("TELEOP_TRIGGER_RATE_PER_S", "2.0")),
                   max_dt_s=float(env.get("TELEOP_TRIGGER_MAX_DT_S", "0.1")))

    @staticmethod
    def _target(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return 0.0
        return min(1.0, max(0.0, value)) if _math.isfinite(value) else 0.0

    def step(self, side, target, now=None):
        target = self._target(target)
        now = time.monotonic() if now is None else float(now)
        pending = self._pending.get(side)
        if pending is not None:
            baseline = pending.get("baseline")
            if baseline is None:
                pending["baseline"] = target
                self._time[side] = now
                return None
            if abs(target - baseline) < 0.02:
                self._time[side] = now
                return None
            del self._pending[side]
            self._time[side] = now
            return self._value[side]
        if self.rate_per_s <= 0:
            self._value[side] = target
            return target
        prior_t = self._time.get(side)
        self._time[side] = now
        if prior_t is None:
            self._value[side] = target
            return target
        dt = min(self.max_dt_s, max(0.0, now - prior_t))
        current = self._value[side]
        delta = min(self.rate_per_s * dt, abs(target - current))
        current += delta if target >= current else -delta
        self._value[side] = current
        return current

    def prepare(self, side, current=None):
        """Hold a hand through arming until its trigger is deliberately moved."""
        if current is not None:
            self._value[side] = self._target(current)
        self._pending[side] = {"baseline": None}
        self._time.pop(side, None)

    def status(self):
        return {"rate_per_s": self.rate_per_s, "full_travel_s":
                (round(1.0 / self.rate_per_s, 3) if self.rate_per_s > 0 else 0.0),
                "max_dt_s": self.max_dt_s, "value": dict(self._value),
                "holding_for_trigger_move": sorted(self._pending)}


def grip_fraction(side, q):
    """Estimate the scalar grip fraction represented by measured Dex3 joints."""
    if not isinstance(q, (list, tuple)) or len(q) != 7:
        return None
    opened, closed = grip_q(side, 0.0), grip_q(side, 1.0)
    fractions = []
    for value, lo, hi in zip(q, opened, closed):
        span = hi - lo
        if abs(span) < 1e-9:
            continue
        try:
            f = (float(value) - lo) / span
        except (TypeError, ValueError):
            continue
        if _math.isfinite(f):
            fractions.append(min(1.0, max(0.0, f)))
    if not fractions:
        return None
    fractions.sort()
    return fractions[len(fractions) // 2]



def grip_q(side, f):
    """0..1 -> the 7 Dex3 joint angles for that hand, open to closed.

    One mapping for every scalar grip input: a
    controller's trigger. Interpolates each joint from its OPEN end to its
    CLOSED end, which are the two ends of the joint's own range and are
    mirrored between hands -- so `f` means the same thing on both.

    OPEN is zero, clamped into range -- a flat neutral hand, which is what the
    Dex3's ranges are built around and what the finger calibration also treats
    as open. Taking the near end of the range instead splays the thumb:
    thumb_1 runs -0.72..0.92, so its "near end" is 41 deg of rotation on a
    hand that is meant to be open. CLOSED is whichever end is further from
    open: the ranges run outward from that neutral pose, mirrored between
    hands, so this picks the curl direction on both. thumb_0 rotates the
    thumb ACROSS the palm rather than curling, and its range is symmetric
    about zero; curling it does not help a grasp, so it stays neutral, where
    a hand holding something has it.
    """
    try:
        f = float(f)
    except (TypeError, ValueError):
        f = 0.0
    if f != f:                      # NaN
        f = 0.0
    f = min(1.0, max(0.0, f))
    names = DEX3_LEFT if side == "left" else DEX3_RIGHT
    out = []
    for n in names:
        lo, hi = DEX3_RANGE[n]
        neutral = min(hi, max(lo, 0.0))
        closed = hi if abs(hi - neutral) >= abs(lo - neutral) else lo
        # OPEN is the far end from closed, dialled by THUMB_OPEN.
        #
        # This was `min(hi, max(lo, 0.0))` -- zero clamped into the range --
        # which IS the open end for every joint whose range stops at zero,
        # and every joint here does except thumb_1. Its range straddles zero
        # (left -0.724..+0.920, right -0.920..+0.724), so zero is the MIDDLE
        # of its travel: a released trigger brought the thumb half way back
        # and the extending half was never commanded. Measured: every other
        # joint used 100 % of its range, thumb_1 used 56 %.
        #
        # The operator on controllers: "the thumb is so close to the body and
        # cant extend as is the default and stays close" -- which is exactly
        # this, the f=0 pose being a half-open hand rather than an open one.
        #
        # Against that stands a deliberate earlier call (test_open_is_a_
        # neutral_hand_not_a_splayed_one): the near end is 41 deg of thumb
        # rotation on a hand meant to be open, and that looks splayed. That
        # was a prediction made off the robot; the complaint is a report from
        # on it, so the default follows the operator -- but the old pose is
        # one curl away rather than deleted, because which of the two GRIPS
        # is a fact about this hand nobody has measured yet. Same reasoning,
        # and same shape, as GRIP_THUMB0 below.
        far = lo if closed == hi else hi
        opened = neutral + THUMB_OPEN * (far - neutral) if n.endswith("thumb_1_joint") else far
        if n.endswith("thumb_0_joint"):
            # thumb_0 rotates the thumb ACROSS the palm rather than curling it,
            # and was pinned neutral on the reasoning that curling it does not
            # help a grasp. For a CYLINDER it might: rotating the thumb across
            # the palm is opposition, which is what holds a can.
            #
            # Measured with the operator driving controllers: every other joint
            # used 100 % of its range and this one used 0 %, and the operator
            # reported the palm not opening or closing fully. On the hand path
            # the same joint used 100 %, so the controller grip was strictly
            # poorer than the fingers it replaced.
            #
            # Which SIGN opposes is a fact about this hand that nobody here
            # knows, and guessing it is how this project loses evenings. So:
            # 0.0 keeps the old behaviour exactly, and the operator can dial it
            # either way and say which one grips. Mirrored between hands by the
            # range, as everything else here is.
            # `neutral`, not `opened`: this joint rotates the thumb across
            # the palm and does not open or close anything. Pinned at zero
            # unless the operator dials GRIP_THUMB0.
            out.append(neutral + f * GRIP_THUMB0 * (hi if GRIP_THUMB0 > 0 else -lo))
            continue
        # (1-f)*a + f*b, not a + f*(b-a): exact at BOTH ends. The second
        # form drifts once `opened` is itself computed (thumb_1 via the dial)
        # and returned 0.9200000000000002 for a closed pose that has to equal
        # CLOSED_LEFT exactly -- real/g1_dex3.py asserts that pair.
        out.append((1.0 - f) * opened + f * closed)
    return out


# How far the thumb OPENS when the trigger is released, as a fraction of the
# travel between neutral and thumb_1's extending limit. thumb_1 is the only
# Dex3 joint whose range straddles zero, so it is the only one with a choice.
#
#   0.0  neutral -- the pose before 2026-09-16. Half the joint's travel, and
#        the operator's complaint: "the thumb ... cant extend ... stays close"
#   0.5  -0.36 rad (~21 deg) open. THE DEFAULT, and a starting point, not an
#        answer
#   1.0  -0.72 rad (~41 deg): the URDF limit
#
# 1.0 is NOT assumed safe. The URDF number is where the MOTOR stops, not
# where the thumb stops hitting something -- the operator's reply to a full
# extension was "if it opens all the way then it hits the body", and nothing
# in a joint limit knows about the palm, the index finger or the torso. The
# earlier `test_open_is_a_neutral_hand_not_a_splayed_one` was most likely
# this same observation written as appearance rather than contact.
#
# So the default sits in the middle and the number is found on the robot:
# dial up until the thumb touches, then back off. Live-tunable for exactly
# that, and recorded in every session header so a run can say which pose it
# used. Same shape as GRIP_THUMB0 below, and for the same reason: this is a
# fact about this hand, and guessing it is how this project loses evenings.
THUMB_OPEN = 0.5


def set_thumb_open(v):
    """-> the value set. 0 = the old neutral hand, 1 = the thumb extended."""
    global THUMB_OPEN
    v = float(v)
    if not _math.isfinite(v) or not 0.0 <= v <= 1.0:
        raise ValueError("thumb_open must be a finite number in [0, 1]")
    THUMB_OPEN = v
    return THUMB_OPEN


# How much thumb_0 follows the grip: 0 = pinned neutral (the old behaviour),
# +1/-1 = its full travel one way or the other at a full squeeze.
GRIP_THUMB0 = 0.0


def set_grip_thumb0(v):
    """-> the value set. Live-tunable; see grip_q."""
    global GRIP_THUMB0
    v = float(v)
    if not _math.isfinite(v) or not -1.0 <= v <= 1.0:
        raise ValueError("thumb0 must be a finite number in [-1, 1]")
    GRIP_THUMB0 = v
    return GRIP_THUMB0


# A pose must move a joint at least this far from the other pose to count as a
# usable calibration for it. Below this the two extremes are indistinguishable
# and stretching them would amplify noise into full-scale finger motion.
CAL_MIN_SPAN = 0.15


def _wrist_se3(pos_head, quat_head):
    """Wrist pose in head frame -> 4x4 SE3 in robot torso frame."""
    R = _R_XR_TO_ROBOT @ _quat_to_R(quat_head) @ _R_XR_TO_ROBOT.T
    t = _R_XR_TO_ROBOT @ (np.asarray(pos_head) * _REACH_SCALE) + _PELVIS_TO_HEAD
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = t
    return T


class ArmIKWorker:
    """Runs the arm IK off the frame loop, on the NEWEST target only.

    The solve costs ~95 ms (measured). Called inline, it set the rate for
    everything: 85 fps of hand data went in and 10.6 frames/s came out, so the
    fingers -- which need no IK at all -- ran eight times slower than the
    headset could drive them.

    Two decisions, both about what to do with the time:

    Only the latest target is kept. A queue would be worse than useless here:
    every solve would be answering a question the operator has already moved
    on from, and the backlog would grow without bound. Submitting overwrites.

    A solve that fails leaves the previous solution in place. An arm that keeps
    its last pose is a stale arm; an arm handed an IPOPT failure is an arm
    commanded somewhere nobody asked for.

    The cost is honest latency: arm targets lag the hands by up to one solve.
    That is the price of the fingers being live, and it is the right trade --
    fingers do the grasping, and a grasp that responds late feels broken in a
    way a slightly-late elbow does not.
    """

    def __init__(self, ik, q0):
        self._ik = ik
        self._pending = None
        self._q = np.asarray(q0, float)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self.n_solved = 0
        self.n_failed = 0
        self.n_dropped = 0           # targets superseded before being solved
        self.last_ms = 0.0
        self.solved_t = 0.0
        self._stop = False
        self.affinity = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="arm-ik")
        self._thread.start()

    def submit(self, Tl, Tr):
        with self._lock:
            if self._pending is not None:
                self.n_dropped += 1
            self._pending = (Tl, Tr)
        self._wake.set()

    def latest(self):
        """The most recent solution, and how old it is in seconds."""
        with self._lock:
            return self._q.copy(), (time.monotonic() - self.solved_t
                                    if self.solved_t else None)

    def stats(self):
        with self._lock:
            age = time.monotonic() - self.solved_t if self.solved_t else None
        return {"solved": self.n_solved, "failed": self.n_failed,
                "dropped": self.n_dropped, "last_ms": round(self.last_ms, 1),
                "age_s": round(age, 3) if age is not None else None,
                "affinity": self.affinity}

    def stop(self):
        self._stop = True
        self._wake.set()

    def _run(self):
        # Pin only the solver worker to a dedicated Thor core. Pinocchio/CasADi
        # stay CPU-native; moving this tiny 14-DoF solve to a GPU would add
        # transfer/launch overhead and neither library has a GPU backend here.
        # TELEOP_IK_CPU=off restores normal scheduling; an integer chooses.
        requested = os.environ.get("TELEOP_IK_CPU", "auto").strip().lower()
        try:
            if requested not in ("", "off", "none") and hasattr(os, "sched_setaffinity"):
                available = sorted(os.sched_getaffinity(0))
                cpu = available[-1] if requested == "auto" else int(requested)
                if cpu not in available:
                    raise ValueError(f"expected one of {available}")
                os.sched_setaffinity(0, {cpu})
                self.affinity = {"requested": requested, "cpu": cpu,
                                 "active": True, "error": None}
            else:
                self.affinity = {"requested": requested or None, "cpu": None,
                                 "active": False, "error": None}
        except (OSError, ValueError) as exc:
            self.affinity = {"requested": requested, "cpu": None,
                             "active": False, "error": str(exc)}
        while not self._stop:
            self._wake.wait(0.5)
            self._wake.clear()
            with self._lock:
                job, seed = self._pending, self._q.copy()
                self._pending = None
            if job is None:
                continue
            t0 = time.monotonic()
            try:
                q, _ = self._ik.solve_ik(job[0], job[1], seed)
                ok = q is not None and np.all(np.isfinite(q))
            except Exception as e:                  # IPOPT can raise
                print(f"[retarget] arm IK failed: {e}")
                ok = False
            dt = (time.monotonic() - t0) * 1e3
            with self._lock:
                self.last_ms = dt
                if ok:
                    self._q = np.asarray(q, float)
                    self.solved_t = time.monotonic()
                    self.n_solved += 1
                else:
                    self.n_failed += 1


class G1Retargeter:
    def __init__(self):
        # Heavy imports live here so `import retarget_g1` stays cheap.
        from dex_retargeting.retargeting_config import RetargetingConfig
        from _vendor_robot_arm_ik import G1_29_ArmIK

        with open(_DEX3_YML) as f:
            cfg = yaml.safe_load(f)

        # Retargeting type and scale, tunable without a code change.
        #
        # Unitree ships type: DexPilot, whose cost matches fingertip-to-thumb
        # and wrist-to-fingertip VECTORS. Measured on this robot with perfect
        # input (zero fabricated landmarks, 25/25 joints seen at 90 fps), it
        # saturates: 9 of 14 joints sat at a limit regardless of hand pose, and
        # the 5 that moved used about half their range.
        #
        # `vector` is the alternative in Unitree's own config. It maps
        # wrist-to-fingertip positions directly and honours scaling_factor,
        # which DexPilot ignores -- and scale is the obvious suspect when a
        # human hand is driving a smaller one.
        want = os.environ.get("TELEOP_RETARGET_TYPE", "").strip()
        scale = os.environ.get("TELEOP_FINGER_SCALE", "").strip() or str(FINGER_SCALE)
        for side in ("left", "right"):
            if want:
                cfg[side]["type"] = want
                if want.lower() == "vector":
                    # the vector path reads its own index list
                    v = cfg[side].get("target_link_human_indices_vector")
                    if v is not None:
                        cfg[side]["target_link_human_indices"] = v
            if scale:
                cfg[side]["scaling_factor"] = float(scale)
        if want or scale:
            print(f"[retarget] finger retargeting: type={want or 'DexPilot'} "
                  f"scaling_factor={scale or cfg['left'].get('scaling_factor')}")
        RetargetingConfig.set_default_urdf_dir(_ASSETS)

        self._left = RetargetingConfig.from_dict(_known_keys(cfg["left"])).build()
        self._right = RetargetingConfig.from_dict(_known_keys(cfg["right"])).build()
        self._li = self._left.optimizer.target_link_human_indices
        self._ri = self._right.optimizer.target_link_human_indices
        self._load_finger_cal()
        self._load_wrist_sign()
        self._rot_l, self._rot_r = _finger_rot("left"), _finger_rot("right")
        self._trigger_grip = TriggerGripSlew.from_env()
        print(f"[retarget] controller trigger grip: {self._trigger_grip.status()}")
        print(f"[retarget] finger frame rotation: left={self._rot_l.flatten().tolist()}")
        self._l_to_hw = [self._left.joint_names.index(n) for n in DEX3_LEFT]
        self._r_to_hw = [self._right.joint_names.index(n) for n in DEX3_RIGHT]

        self._arm_ik = G1_29_ArmIK(Unit_Test=False, Visualization=False)
        self._arm_q = None  # warm-start for the QP

        # AFTER the IK exists, not before. This block sat above the two lines
        # it reads, so TELEOP_ASYNC_IK=1 raised AttributeError and took the
        # bridge down at startup -- a total failure of a shipped flag. It never
        # showed up here because the flag cannot be exercised on a Mac at all:
        # G1_29_ArmIK needs pinocchio.casadi, which pip cannot provide, so
        # __init__ dies earlier for an unrelated reason and the ordering bug is
        # invisible. Found by review, not by us.
        #
        # Off by default so nothing changes underneath the modes already
        # verified on hardware; forearm and fingers-only never reach this path.
        if os.environ.get("TELEOP_ASYNC_IK", "") not in ("", "0"):
            self._ik_worker = ArmIKWorker(self._arm_ik, self._arm_q)
            print("[retarget] async arm IK: fingers run at headset rate, arm "
                  "targets lag by up to one solve")

        # Last GENUINELY COMPUTED targets. Starts empty, and that is the point.
        #
        # This used to be seeded with zeros "so a missing hand holds still
        # instead of snapping". Zeros are not "hold still" -- zero is a specific
        # arm pose. The arm IK needs BOTH wrists, so a single tracked hand (a
        # routine, expected condition: the other hand drops out of view for a
        # moment) skipped the IK and emitted a complete, confident, 90 fps
        # command to drive both arms to that zero pose. Every downstream guard
        # passed it, because a fabricated target is indistinguishable from a
        # real one once it has been written into the dict.
        #
        # Empty instead means those joints are simply ABSENT until they have
        # been solved for once, and the sink's existing completeness check drops
        # the frame -- so the robot sees no data and freezes, which is the
        # correct response to "we do not know where the arms should go".
        # Once a two-handed frame HAS been solved, falling back to it really
        # does mean hold still: it is where the arms already are.
        self._last = {}

    def prepare_controller_grips(self, measured=None):
        """Keep measured hand poses through arming; triggers take over on motion."""
        slew = getattr(self, "_trigger_grip", None)
        if slew is None:
            slew = self._trigger_grip = TriggerGripSlew.from_env()
        measured = measured if isinstance(measured, dict) else {}
        for side in ("left", "right"):
            slew.prepare(side, grip_fraction(side, measured.get(side)))

    @staticmethod
    def _apply_cal(joint, v, open_v, closed_v):
        """Stretch [open_v, closed_v] onto the joint's real [open end, closed end].

        Clamped at both ends: a hand posed slightly beyond what was recorded
        must not command past the joint limit.
        """
        lo, hi = DEX3_RANGE[joint]
        # Which limit is "closed" is whichever end the closed pose sat nearer.
        closed_end, open_end = (hi, lo) if abs(closed_v - hi) < abs(closed_v - lo) else (lo, hi)
        span = closed_v - open_v
        if abs(span) < CAL_MIN_SPAN:
            return v                      # not enough separation to trust
        t = (v - open_v) / span           # 0 at the operator's open, 1 at closed
        t = min(1.0, max(0.0, t))
        return open_end + t * (closed_end - open_end)

    def _load_finger_cal(self):
        if os.environ.get("TELEOP_FINGER_CAL", "") in ("0", "off", "none"):
            print("[retarget] finger calibration disabled by env")
            return
        try:
            blob = json.load(open(self._CAL_FILE))
        except (OSError, ValueError):
            return
        cal = {k: (float(v[0]), float(v[1])) for k, v in blob.get("joints", {}).items()}
        if cal:
            self._cal = cal
            print(f"[retarget] loaded finger calibration for {len(cal)} joints "
                  f"from {self._CAL_FILE}")

    # Operator wrist orientation -> the robot's three wrist joints.
    # Derived the same way the finger rotation was: empirically, against a
    # recording. _WRIST_ROT is the starting guess; an offline sweep
    # replaces it with whatever the measurement says.
    # C: robot-hand frame <- XR-wrist frame. DERIVED, not swept
    # (derived offline): both frames are expressed in terms of the
    # same two anatomical directions -- wrist to the knuckle midpoint, and
    # index knuckle to middle knuckle -- read from 2525 recorded frames on the
    # operator side and from the URDF on the robot side, matching fingers BY
    # NAME so no radial/ulnar guess is involved.
    #
    # The 24-candidate sweep could not do this: its score (joint range minus
    # clipping) is blind to direction, so its top four were sign-flips of each
    # other with identical scores. Reassuringly, the derived matrices snap to
    # two of those four, so the two methods agree where the sweep could see at
    # all. Left and right came out exact mirrors of one another without that
    # being imposed, which is the check that they are not noise.
    #
    # Kept un-snapped: the hand is ~13 deg off axis-aligned to the wrist, and
    # rounding that away would bake in a 13 deg bias.
    _WRIST_ROT = {
        "left":  np.array([[+0.1224, -0.0883, -0.9885],
                           [+0.2185, +0.9740, -0.0599],
                           [+0.9681, -0.2087, +0.1385]], float),
        "right": np.array([[-0.1224, -0.0883, -0.9885],
                           [+0.2185, -0.9740, +0.0599],
                           [-0.9681, -0.2087, +0.1385]], float),
    }
    _WRIST_ROT = {k: _so3(v) for k, v in _WRIST_ROT.items()}

    # A controller's gripSpace is not the hand's wrist frame, and the two are
    # defined precisely enough to line up on paper. OpenXR grip (WebXR's
    # gripSpace follows it): -Z runs through the tube the curled fingers make,
    # little finger toward thumb -- out of the top of the fist; +X is the palm
    # normal, into the palm for the right hand and away from it for the left
    # (so it points the same way, across the body, for both); +Y completes the
    # right-handed frame. WebXR hand wrist joint: -Z toward the fingers, +Y out
    # of the back of the hand, +X completing the frame.
    #
    # For a fist holding a controller thumb-up (knuckles forward, back of the
    # hand outward), in world terms with F forward, U up, R the user's right:
    #   grip  right: X=R   Y=-F  Z=-U      hand right: X=-U  Y=R   Z=-F
    #   grip  left:  X=R   Y=-F  Z=-U      hand left:  X=U   Y=-R  Z=-F
    # so the hand frame in grip coordinates, D with R_hand = R_grip @ D, is a
    # cyclic permutation of the axes, mirrored between hands (D_left is
    # diag(-1,1,1) D_right diag(-1,1,1); a test pins it). Applied here, where
    # the wrist quaternion enters, so the bridge has ONE wrist convention:
    # the zero-referenced path, C, the sign flips and the recordings all stay
    # as they are. Kept in the bridge rather than the headset on purpose --
    # recordings then carry the RAW controller orientation, and if hardware
    # disagrees with this derivation the fix is a constant here, derivable
    # from a recording the same way C was.
    #
    # NOT verified on hardware. A wrong D shows up as an axis swap -- pitching
    # the controller rolls the robot's wrist -- and the check is the runbook's:
    # zero, then roll, pitch and yaw the controller one axis at a time.
    _GRIP_TO_WRIST = {
        "right": np.array([[0.0, 1.0, 0.0],
                           [0.0, 0.0, 1.0],
                           [1.0, 0.0, 0.0]]),
        "left":  np.array([[0.0, -1.0, 0.0],
                           [0.0,  0.0, 1.0],
                           [-1.0, 0.0, 0.0]]),
    }
    # Wrist axes, plus the shoulder's two. The shoulder pair is here for the
    # same reason the wrists are: the geometry checks out against the URDF to
    # 0.0013 deg, and the OPERATOR reported the shoulder going backwards when
    # they reached forwards. Hardware is the authority -- either the URDF's
    # axis sign does not match the real joint, or the frame mapping is flipped
    # for directions, and one live test settles it either way.
    _WRIST_SIGN = {"left": {"roll": 1.0, "pitch": 1.0, "yaw": 1.0,
                            "shoulder_pitch": 1.0, "shoulder_roll": 1.0,
                            "shoulder_yaw": 1.0},
                   "right": {"roll": 1.0, "pitch": 1.0, "yaw": 1.0,
                             "shoulder_pitch": 1.0, "shoulder_roll": 1.0,
                             "shoulder_yaw": 1.0}}

    _SIGN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "calibration", "wrist_sign.json")

    _wrist_ref = None          # {side: R_0}, the pose the operator zeroed at
    _forearm_ref = None        # {side: F_0}, the robot forearm at that same moment
    # Bumped every time the reference changes (captured or cleared), so the
    # session recorder can tell "same zero as last frame" from "new zero"
    # without comparing matrices on the hot path.
    _ref_version = 0
    _ref_cache = None

    def _bump_ref(self):
        self._ref_version = (self._ref_version or 0) + 1
        self._ref_cache = None

    def wrist_reference(self):
        """-> {"v": int, "sides": {side: {"R0": 3x3 list, "F0": 3x3 list | None}}}

        The wrist joints are RELATIVE to this. Every wrist target in a session
        is meaningless without it, and until it was recorded a replay could
        only re-zero on the first frame and disagree with the recording by up
        to 170 deg on wrist roll while matching every shoulder and elbow to
        the digit -- which is what 20260915-001254-first-full-stack.jsonl
        showed, and why nobody could tell a wrist bias from a bad zero pose
        after the fact. Cached until the reference changes: this is read once
        per frame at 90 fps.
        """
        if self._ref_cache is None:
            sides = {}
            for side, R0 in (self._wrist_ref or {}).items():
                F0 = (self._forearm_ref or {}).get(side)
                sides[side] = {"R0": np.asarray(R0, float).tolist(),
                               "F0": None if F0 is None else np.asarray(F0, float).tolist()}
            self._ref_cache = {"v": int(self._ref_version or 0), "sides": sides}
        return self._ref_cache

    def set_wrist_reference(self, ref):
        """Install a reference as recorded by wrist_reference() -- for replay.

        Sides the recording had not captured yet stay absent, so they are
        captured lazily on the next frame exactly as they were live.
        """
        self._wrist_ref, self._forearm_ref = {}, {}
        for side, d in ((ref or {}).get("sides") or {}).items():
            self._wrist_ref[side] = np.asarray(d["R0"], float)
            if d.get("F0") is not None:
                self._forearm_ref[side] = np.asarray(d["F0"], float)
        # A new reference is a discontinuity on purpose, as in zero_wrists().
        self._wrist_prev = {}
        self._gap_runs = {}
        self._ref_version = int((ref or {}).get("v") or 0)
        self._ref_cache = None

    def _wrist_zero(self, side, R_w):
        """The reference orientation for `side`, captured on first use."""
        if self._wrist_ref is None:
            self._wrist_ref = {}
        if side not in self._wrist_ref:
            self._wrist_ref[side] = R_w
            self._bump_ref()
            print(f"[retarget] wrist zero captured for {side}")
        return self._wrist_ref[side]

    def _forearm_zero(self, side, R_w, F):
        """-> (R_0, F_0): the operator's hand AND the robot's forearm at zero.

        Captured together, on one frame, because they describe one moment.
        If the forearm reference is missing the wrist reference is re-taken
        with it, even if an earlier frame captured one on the constant path
        (the shoulder solve returns nothing with the hand at the shoulder, and
        that frame falls through to the constant path). A zero that paired R_0
        from one frame with F_0 from another would command the difference
        between the two moments as a standing offset -- the class of bug the
        zero exists to remove.
        """
        if self._wrist_ref is None:
            self._wrist_ref = {}
        if self._forearm_ref is None:
            self._forearm_ref = {}
        if side not in self._forearm_ref:
            self._forearm_ref[side] = F
            self._wrist_ref[side] = R_w
            self._bump_ref()
            print(f"[retarget] wrist + forearm zero captured for {side}")
        return self._wrist_ref[side], self._forearm_ref[side]

    def zero_wrists(self):
        """Re-capture the reference on the next frame of each hand.

        Do this right before arming, with the hands held in the pose the
        robot's arms are actually in. Zeroing is what makes the mapping a
        change in wrist BEND rather than a reading of where the hand points in
        the room, so an operator who turns around does not drag the wrists.
        """
        self._wrist_ref = {}
        self._forearm_ref = {}
        self._wrist_prev = {}
        self._gap_runs = {}
        self._bump_ref()
        return "wrist zero cleared — next frame of each hand becomes the reference"

    def flip_wrist(self, side, axis):
        if side not in self._WRIST_SIGN or axis not in self._WRIST_SIGN[side]:
            return (f"refused: bad side/axis {side}/{axis}; axes are "
                    f"{sorted(self._WRIST_SIGN['left'])}")
        # Copy rather than mutate: _WRIST_SIGN is a CLASS attribute, and
        # mutating it in place would edit the default that every other
        # instance -- and every later test -- starts from.
        sg = {s: dict(v) for s, v in self._WRIST_SIGN.items()}
        sg[side][axis] *= -1.0
        self._WRIST_SIGN = sg
        # A flip changes the angle discontinuously ON PURPOSE, so the
        # impossible-step gate would reject the very change being asked for and
        # the flip would silently do nothing. Forget the continuity history, as
        # zeroing does.
        self._wrist_prev = {}
        self._gap_runs = {}
        self._save_wrist_sign()
        return f"{side} {axis} now {sg[side][axis]:+.0f}"

    def _save_wrist_sign(self):
        """Persist, for the same reason the finger calibration persists.

        Settling six signs costs the operator six trips into VR. Losing them
        to a redeploy would mean doing that again, which is exactly how the
        finger hunt got expensive.
        """
        try:
            os.makedirs(os.path.dirname(self._SIGN_FILE), exist_ok=True)
            with open(self._SIGN_FILE, "w") as f:
                json.dump({"signs": self._WRIST_SIGN}, f, indent=2, sort_keys=True)
        except OSError as e:
            print(f"[retarget] could not save wrist signs: {e}")

    def _load_wrist_sign(self):
        try:
            blob = json.load(open(self._SIGN_FILE))
        except (OSError, ValueError):
            return
        sg = {s: dict(v) for s, v in self._WRIST_SIGN.items()}
        n = 0
        for side, axes in (blob.get("signs") or {}).items():
            if side not in sg:
                continue
            for axis, v in (axes or {}).items():
                if axis in sg[side] and v in (1, -1, 1.0, -1.0):
                    sg[side][axis] = float(v)
                    n += 1
        if n:
            self._WRIST_SIGN = sg
            # Name what is FLIPPED, not the whole dict. One -1 among ten 1.0s
            # is invisible in a deploy log, and one was: left shoulder_pitch
            # sat at -1 in the shipped file after the shoulder solve it was
            # correcting had been replaced, and every startup printed it.
            flipped = [f"{side} {axis}=-1" for side in ("left", "right")
                       for axis, v in sg[side].items() if v < 0]
            print(f"[retarget] loaded wrist signs from {self._SIGN_FILE}: "
                  + (", ".join(flipped) if flipped else "no flips, all +1"))
    # Operator head -> shoulder, in the head's heading frame (x right, y up,
    # -z forward), metres. WebXR reports no shoulder, so one has to be
    # assumed, and the assumption sets where "across the body" begins: the
    # shoulder direction is mapped 1:1 onto the robot, so a hand at the
    # operator's midline lands wherever that direction points from the
    # ROBOT's shoulder.
    #
    # DERIVED from the URDF rather than typed: the robot's own shoulders
    # (+-0.100 m from its midline, 0.292 m above the pelvis) carried back into
    # the operator's head frame through the same offset and reach scale the
    # position path uses. That puts the assumed shoulder 13.5 cm to the side,
    # 24.6 cm below the eyes and 7.3 cm behind them, which is also closer to a
    # person than the old hand-typed (+-0.18, -0.20, 0.0): shoulders are behind
    # the eyes, not level with them.
    #
    # Why it matters: with the shoulder assumed 18 cm out, a hand held at the
    # operator's midline read as 18 cm INWARD of the shoulder and the robot was
    # told to point its arm 18 cm inward of a shoulder 10 cm from its midline
    # -- across its chest. Dry-run on the first full-stack episode, right hand:
    # unreachable shoulder solutions 58 % with the old guess, 16 % with the yaw
    # search alone, 4 % with both. The reach calibration absorbs the constant
    # change in distance, as it always has.
    _SHOULDER = {
        "left": np.linalg.inv(_R_XR_TO_ROBOT) @ ((_SHOULDER_L - _PELVIS_TO_HEAD) / _REACH_SCALE),
        "right": np.linalg.inv(_R_XR_TO_ROBOT) @ ((_SHOULDER_R - _PELVIS_TO_HEAD) / _REACH_SCALE),
    }
    # RIGHT: measured, not derived. The operator put their own hand and the
    # ROBOT's hand in the same place at three poses -- tucked, L-shape, full
    # reach -- with the arm limp, and both were recorded. Their idea, and it
    # caught what no offline check had: the derived shoulder says a hanging
    # arm is 0.295 m from the shoulder and an extended arm 0.427 m. Both are
    # STRAIGHT arms, so both must give the same distance -- an arm does not
    # change length when you point it somewhere else. 13 cm of disagreement,
    # and it is why the elbow was commanded 21-77 deg more bent than the
    # operator wanted at every reach ("it never extends").
    #
    # Solved from those three poses against the robot's own FK: RMS residual
    # 8.6 cm with the derived shoulder, 2.7 cm with this one. Left is
    # untouched -- only the right was measured, and a guess mirrored from one
    # arm's measurement is still a guess.
    #
    # Applied MIRRORED to both sides. Only the right was measured, but the
    # operator has one pair of shoulders and they are symmetric, whereas the
    # left/right mirror symmetry of this whole solve is a property several
    # tests rely on to catch frame bugs -- it is what proved the wrist was not
    # mirrored wrongly when a recording said it was. Keeping a measured right
    # beside a derived left would trade that invariant for nothing: the left
    # value is a guess either way, and this guess has evidence behind it.
    _SHOULDER["right"] = np.array([0.120, -0.150, 0.059])
    _SHOULDER["left"] = _SHOULDER["right"] * np.array([-1.0, 1.0, 1.0])

    # Shoulder-to-wrist distance, in metres, at the two ends of the operator's
    # comfortable reach. Defaults are the 5th/95th percentiles measured over a
    # 4000-frame session; POST /elbowcal/{near,far} replaces them per operator.
    # RIGHT re-fitted against the corrected shoulder above, from the same
    # three matched poses: predicts the elbow the operator actually set to
    # 5.3 deg RMS, where the old pair was 21-77 deg too bent.
    _ELBOW_CAL = {"left": (0.160, 0.392), "right": (0.160, 0.392)}

    # The robot's elbow is STRAIGHT at about +73 deg, not at zero: at q=0 the
    # included angle between upper arm and forearm is already 106.8 deg, and it
    # folds in both directions from there (derived from the URDF link offsets,
    # shoulder_yaw->elbow and elbow->wrist_roll). So the usable monotonic span
    # runs from the lower limit up to straight, and "more reach" means "larger
    # q" across all of it.
    _ELBOW_SPAN = (-1.0472, 1.2776)        # most bent .. straight, radians

    def _elbow_q(self, side, hand):
        """-> the elbow joint angle, from how far the hand is from the shoulder.

        The elbow angle is set by shoulder-to-wrist distance through the law of
        cosines, so distance is the signal -- but only measured from the
        SHOULDER. An earlier version used distance from the HEAD, saw it vary
        by 7 cm, and concluded the elbow was not recoverable. That was measured
        on a session of pure wrist rotation with no reaching in it, and it
        generalised from the absence of the motion in question. Measured
        properly, over a session that does contain reaching: 31 cm of span, 77
        deg of implied elbow swing, and a p99 frame-to-frame step of 1.5 deg --
        a signal-to-noise ratio of about 50:1.

        Linear in distance rather than the exact law of cosines, because the
        arm-length and shoulder-position constants that the exact form needs
        are precisely the ones we do not have; the calibration measures the
        operator's actual endpoints instead.
        """
        w = hand.get("wrist")
        if w is None:
            return None
        d = float(np.linalg.norm(np.asarray(w, float) - self._SHOULDER[side]))
        near, far = self._ELBOW_CAL[side]
        if far - near < 0.05:
            return None                     # calibration too tight to trust
        t = min(1.0, max(0.0, (d - near) / (far - near)))
        lo, hi = self._ELBOW_SPAN
        q = lo + t * (hi - lo)
        jlo, jhi = ARM_JOINT_LIMITS[f"{side}_elbow_joint"]
        return min(jhi, max(jlo, q))

    def elbow_cal(self, side, which, hand_wrist):
        """Capture one end of the operator's reach."""
        if side not in self._ELBOW_CAL or which not in ("near", "far"):
            return f"refused: bad side/end {side}/{which}"
        d = float(np.linalg.norm(np.asarray(hand_wrist, float)
                                 - self._SHOULDER[side]))
        near, far = self._ELBOW_CAL[side]
        new = (d, far) if which == "near" else (near, d)
        # Refuse an inverted or too-tight span instead of storing it.
        #
        # Captured live, the usual failure is timing: the capture fires before
        # the operator has actually reached, so `far` lands at or inside
        # `near`. Stored, that silently disables the elbow (the mapping returns
        # None below a 5 cm span) or -- worse if the guard were absent --
        # inverts it, so reaching out would fold the arm. Better to refuse and
        # say why; the previous calibration stays usable.
        span = new[1] - new[0]
        if span < 0.05:
            return (f"refused: {side} span would be {span*100:.1f} cm "
                    f"({new[0]:.3f}..{new[1]:.3f}). Hold the pose, then capture "
                    f"-- an inverted or tiny span disables the elbow rather "
                    f"than calibrating it. Previous calibration kept.")
        cal = dict(self._ELBOW_CAL)
        cal[side] = new
        self._ELBOW_CAL = cal
        return f"{side} {which} = {d:.3f} m (now {cal[side][0]:.3f}..{cal[side][1]:.3f})"

    # What a HUMAN wrist can actually do, in radians. Clinical ranges, rounded
    # down a little: pronation/supination ~85 deg each way, flexion/extension
    # ~80, radial/ulnar deviation ~35.
    #
    # These are tighter than the robot's joint limits on purpose. The mapping
    # reads the operator's hand orientation relative to their HEAD, and WebXR
    # reports no elbow or shoulder, so rotating the whole arm looks exactly
    # like rotating the wrist. With the robot's shoulder HELD, all of that
    # whole-arm rotation gets funnelled into a wrist that has to represent it
    # alone -- and it saturates. Measured on hardware: both wrist_roll targets
    # swept 226.0 deg, which is the joint's entire range, while a human wrist
    # cannot pass about +-90.
    #
    # So anything beyond human range is arm motion wearing a wrist's clothes.
    # Clamping here keeps it out of the robot rather than letting the onboard
    # leash absorb it, which it does by dragging EVERY joint 10 deg behind --
    # one bad axis making the whole arm feel mushy.
    #
    # This is a symptom fix and is meant to be. The cause is a held shoulder,
    # and it goes away properly when the shoulder is driven too.
    #
    # ROLL raised 1.48 -> 1.92 (85 -> 110 deg) once the shoulder was driven,
    # which is exactly the condition the paragraph above says retires the
    # tight version of this clamp. Measured on the can-grasping session: the
    # operator's roll sat AT the 85 deg clamp on 11.9 % of armed frames while
    # the robot's own limit is 113 deg and was never reached -- so the clamp,
    # not the robot, was the thing refusing to follow their hand. Pitch and
    # yaw are left alone; neither was saturating.
    HUMAN_WRIST = {"roll": 1.92,      # ~110 deg, the robot's own limit is 113
                   "pitch": 1.40,     # ~80 deg
                   "yaw": 0.61}       # ~35 deg

    # A wrist angle cannot change by this much between two frames arriving at
    # ~90 fps. 40 deg in 11 ms is 3600 deg/s; a human wrist peaks around 1000.
    #
    # This exists because the Euler decomposition goes briefly ill-conditioned
    # and the roll flips sign -- measured on a real session: 4 frames out of
    # 3850, all in one 5-frame burst, roll snapping +84.8 -> -84.8 -> +84.8
    # while pitch glided smoothly through -45..-52 and yaw never moved. Not
    # gimbal lock (pitch nowhere near 90) and not wraparound (yaw would jump
    # too): just a representation that briefly has two valid answers.
    #
    # Rare, but the onboard cannot shrug it off. A 169 deg step becomes a slew-
    # limited crawl in the wrong direction, and the tracking leash then holds
    # EVERY joint 10 deg behind while it unwinds -- one bad axis for a few
    # frames making the whole arm feel mushy for far longer.
    #
    # Holding the previous value is the right response: for a handful of frames
    # the operator has effectively not moved, which is true, rather than having
    # teleported, which is not.
    MAX_WRIST_STEP = 0.70              # radians, ~40 deg

    # The elbow needs a tighter bound than the wrists, because it is derived
    # from the wrist POSITION and a dropout-and-reacquire moves that position
    # a long way in one frame. Measured by replaying a real session: p99 step
    # 3.3 deg, max 29.2 -- and 29 deg in 11 ms is 2600 deg/s, where a human
    # elbow peaks near 800. 15 deg is still 1350 deg/s, four times the p99, so
    # it rejects the impossible without touching a fast reach.
    # Per-axis, because "impossible" differs by joint and by how much mass is
    # hanging off it. Dry-running a real session through the shoulder
    # (an offline replay with the shoulder on) produced single-frame steps of 39.5 deg --
    # 3600 deg/s, and on a shoulder that swings the entire arm rather than a
    # hand. The 40 deg wrist threshold let it straight through.
    #
    # A human shoulder peaks near 500 deg/s and an elbow near 800, so 10 and 15
    # deg per frame are already generous at 90 fps. Both are several times the
    # p99 step measured on real motion (0.9 deg shoulder, 3.3 elbow), so they
    # reject the impossible without touching a fast reach.
    MAX_STEP = {"elbow": 0.26,                        # ~15 deg
                "shoulder_pitch": 0.17,               # ~10 deg
                "shoulder_roll": 0.17,
                "shoulder_yaw": 0.17}

    _wrist_prev = None                 # {side: {axis: last good angle}}
    _gap_runs = None                   # {side: {axis: consecutive rejections}}

    # How many consecutive rejections before the new value is believed.
    #
    # A decomposition glitch lasts one or two frames; a hand that reappears
    # somewhere else after a tracking dropout stays there. Holding through the
    # first is right, holding through the second freezes the joint -- which is
    # exactly what happened: _degap returned `last` without updating it, so
    # every later frame was compared against the same stale value and rejected
    # for ever. A recorded session shows a left elbow frozen for 4.3 s and a
    # right wrist yaw frozen for the rest of the run.
    #
    # Three frames is ~33 ms at 90 fps: long enough that no single glitch gets
    # through, short enough that a dropout costs a blink rather than the
    # session.
    MAX_CONSEC_GAPS = 3

    def _degap(self, side, axis, v):
        """Reject a physically impossible step; keep the last good angle.

        Unless the "impossible" value persists, in which case it is simply
        where the operator now is and the gate must yield.
        """
        if self._wrist_prev is None:
            self._wrist_prev = {}
        if self._gap_runs is None:
            self._gap_runs = {}
        prev = self._wrist_prev.setdefault(side, {})
        runs = self._gap_runs.setdefault(side, {})
        last = prev.get(axis)
        # `axis` is "roll"/"pitch"/"yaw" from the wrist path and a full joint
        # name from the shoulder path; match on substring so both find a limit.
        limit = next((lim for k, lim in self.MAX_STEP.items() if k in axis),
                     self.MAX_WRIST_STEP)
        if last is not None and abs(v - last) > limit:
            runs[axis] = runs.get(axis, 0) + 1
            if runs[axis] < self.MAX_CONSEC_GAPS:
                self.n_wrist_gaps += 1
                return last
            if runs[axis] == self.MAX_CONSEC_GAPS:
                # Persisted: this is where the operator is, not a glitch.
                print(f"[retarget] {side} {axis}: converging on a "
                      f"{abs(v - last):.2f} rad gap — tracking gap, not a glitch")
                self.n_wrist_jumps += 1
            # Converge at `limit` per frame rather than teleporting.
            #
            # This branch used to return `v` whole. Measured in the first
            # full-stack episode: right_shoulder_pitch took a 142 deg step in
            # ONE frame, against a p50 step of 0.11 deg. The onboard's 2.5
            # rad/s slew then spent a second sweeping the arm to wherever the
            # glitch pointed, and another sweeping back -- which is what the
            # operator reported as the arm "fighting to move".
            #
            # `runs` is deliberately NOT reset here: every later frame takes
            # this branch too, so the gap closes at a steady `limit` per frame
            # until it is small enough for the normal path below, which resets.
            # At 90 fps even the tightest limit (0.17 rad) is 15 rad/s, six
            # times the onboard slew -- so nothing that tracks normally is
            # slowed by this. Only the spike is removed.
            v = last + _math.copysign(limit, v - last)
            prev[axis] = v
            return v
        runs[axis] = 0
        prev[axis] = v
        return v

    n_wrist_gaps = 0                   # rejected glitches
    n_wrist_jumps = 0                  # accepted jumps -- a dropout, not a glitch

    # The upper arm at zero: elbow position relative to the shoulder-ROLL joint
    # origin, in that joint's frame, every rpy applied. MIRRORED between sides,
    # which is why it cannot be one constant.
    #
    # Measured from the roll origin rather than the pitch origin on purpose.
    # The 4 cm offset between them sits BETWEEN the two joints, so it does not
    # rotate with roll, and including it would make the arm direction something
    # other than a fixed vector turned by pitch and roll -- which is exactly
    # what the closed form needs. Taking the roll origin as the shoulder centre
    # costs a constant 4 cm and buys an exact solution.
    #
    # A first attempt used only the yaw->elbow link and came out 172 deg wrong,
    # caught by checking against URDF forward kinematics rather than against
    # the algebra it came from.
    _UPPER_ARM_0 = {"left": (+0.08554, -0.24196, -0.96651), "right": (+0.08554, +0.24196, -0.96651)}

    # The shoulder-PITCH joint's own rpy, applied BEFORE its rotation. About
    # 16 deg about X, and leaving it out was worth 172 deg of pointing error --
    # the target direction has to be carried into the frame the pitch rotation
    # actually acts in before the closed form can be applied.
    _SHOULDER_PRE = {"left": np.array([[1.0, 0.000199, -0.0], [-0.000192, 0.961246, -0.275692], [-5.5e-05, 0.275692, 0.961246]]), "right": np.array([[1.0, -0.000199, -0.0], [0.000192, 0.961246, 0.275692], [-5.5e-05, -0.275692, 0.961246]])}

    # Robot arm segments, from the URDF: shoulder-roll origin to elbow, and
    # elbow to wrist-roll. Its reach is 0.084..0.285 m -- far shorter than the
    # operator's, which is why reach is CALIBRATED rather than copied.
    _L1 = 0.1845
    _L2 = 0.1005

    # Shoulder yaw joins the solve only when pitch and roll cannot reach
    # inside the robot's limits. Off, _shoulder_q returns exactly the two-joint
    # result and the old wire shape (six joints a side), which is the fallback
    # if the yaw misbehaves on hardware: TELEOP_SHOULDER_YAW=0.
    shoulder_yaw = True
    # The DATASHEET by default. The robot's calibrated limits are a deployment
    # fact: server.py installs them with load_robot_limits,
    # so the geometry tests below see the same closed form they always did.
    ROBOT_LIMITS = ARM_JOINT_LIMITS
    # Per side: how far outside the limits the LAST solution sat before the
    # clamp (0.0 = reachable), and how many frames were clamped. This is the
    # number the first full-stack episode was missing: 58 % of the right
    # shoulder commands were unreachable and nothing on the bridge said so.
    _shoulder_miss = None
    n_shoulder_clamped = 0
    # How far the shoulder may twist to reach somewhere roll alone cannot.
    #
    # Measured on this robot, right arm, with and without the yaw:
    #
    #             elbow follow   shoulder pitch   shoulder roll
    #   yaw ON        116 %           75 %            84 %
    #   yaw OFF        50 %           45 %            42 %
    #
    # so it roughly doubles the arm. But the operator's words with it on were
    # "moves wrong -- goes somewhere I didn't point", and they were describing
    # something real: the HAND lands correctly while the upper arm rotates
    # into a posture nobody asked for, and a limb rotating on its own axis is
    # exactly what looks wrong to the person driving it.
    #
    # So the amount is theirs to choose, live, rather than 70 deg or nothing:
    # set_yaw_max(<degrees>). 0 disables the search entirely
    # and is the six-joint shoulder. The default stays at 70, which is what
    # the measurements above were taken with.
    YAW_SEARCH_MAX = 1.2217         # ~70 deg either way
    YAW_SEARCH_STEP = 0.0873        # ~5 deg grid, then refined to the boundary
    _yaw_prev = None                # {side: last yaw}, to keep the sign stable

    def _limits(self, name):
        return self.ROBOT_LIMITS.get(name) or ARM_JOINT_LIMITS[name]

    @staticmethod
    def _pitch_roll_for(d, w, lim_p, lim_r):
        """Closed form: (pitch, roll, limit miss) putting w_hat onto direction d.

        With the chain pitch(Y) then roll(X) and w = (a, c, b) the direction is

            d = Ry(p) @ Rx(r) @ w_hat
            d_y = c*cos r - b*sin r = R*cos(r + phi),  R = hypot(c, b), phi = atan2(b, c)

        which has TWO roll solutions; both are tried and the reachable one with
        the smaller |roll| wins. `miss` is how far outside the limits the best
        branch sits, in radians; zero means reachable.
        """
        nw = float(np.linalg.norm(w))
        if nw < 1e-6:
            return None
        a, c, b = (w / nw).tolist()
        R = _math.hypot(c, b)
        phi = _math.atan2(b, c)
        k = max(-1.0, min(1.0, float(d[1]) / R)) if R > 1e-9 else 0.0
        (lo_p, hi_p), (lo_r, hi_r) = lim_p, lim_r
        best = None
        for sign in (1.0, -1.0):
            roll = _wrap(sign * _math.acos(k) - phi)
            Z = c * _math.sin(roll) + b * _math.cos(roll)
            pitch = _wrap(_math.atan2(float(d[0]), float(d[2])) - _math.atan2(a, Z))
            miss = (max(0.0, lo_r - roll) + max(0.0, roll - hi_r)
                    + max(0.0, lo_p - pitch) + max(0.0, pitch - hi_p))
            score = (miss, abs(roll))
            if best is None or score < best[0]:
                best = (score, pitch, roll)
        return best[1], best[2], best[0][0]

    def _shoulder_q(self, side, hand):
        """-> {pitch, roll[, yaw]} that put the HAND where the operator's is.

        Two-link analytic IK, not a solver. The vector from shoulder to wrist
        already drives the elbow through its LENGTH; its DIRECTION is exactly
        what the shoulder is for, and it costs nothing to use. The alternative
        -- the vendored IPOPT arm IK -- measured 146-200 ms per solve, which
        sets the rate for the whole pipeline and lands the arm one solve
        behind the hand.

        Pitch and roll come from the closed form in _pitch_roll_for, solved
        against the vector to the hand at the commanded elbow (not to the
        elbow, which was a constant 20.2 deg of error).

        Shoulder YAW is held at exactly zero whenever pitch and roll reach
        inside the robot's calibrated limits: it is the null-space freedom,
        WebXR reports nothing above the wrist, and inventing a yaw would move
        the elbow for reasons the operator could not predict. It moves only
        when the alternative is a command the robot cannot follow. Measured on
        the first full-stack episode: the operator's right hand worked across
        the body for 78 % of the run, the solve asked for up to +77 deg of
        inward roll against a calibrated limit of +5, the arm sat on that limit
        and the leash dragged every right joint behind; the left hand stayed
        beside its shoulder and tracked to 1-2 deg. Yawing the shoulder inward
        swings the arm toward the midline without rolling the upper arm into
        the torso. Dry run on that episode: unreachable right-roll frames
        58 % -> 4 %.

        The yaw search is a 5 deg grid to +-70 deg for the smallest |yaw| that
        reaches, refined by bisection to the reachability boundary so the yaw
        moves continuously with the hand rather than in grid steps; when both
        signs reach, the sign used last frame wins. If nothing reaches, the
        least-miss candidate is taken and clamped, as before.
        """
        w = hand.get("wrist")
        if w is None:
            return {}
        v = np.asarray(w, float) - self._SHOULDER[side]
        n = float(np.linalg.norm(v))
        if n < 0.05:
            return {}                       # hand at the shoulder: no direction
        # Into the frame the pitch rotation acts in, past the joint's own rpy.
        d = self._SHOULDER_PRE[side].T @ (_R_XR_TO_ROBOT @ (v / n))

        e = self._elbow_q(side, hand)
        if e is None:
            return {}                       # no elbow, no consistent shoulder
        e = float(e)
        lim_p = self._limits(f"{side}_shoulder_pitch_joint")
        lim_r = self._limits(f"{side}_shoulder_roll_joint")
        lim_y = self._limits(f"{side}_shoulder_yaw_joint")

        def attempt(yaw):
            sol = self._pitch_roll_for(d, shoulder_to_wrist(side, e, yaw), lim_p, lim_r)
            if sol is None:
                return None
            miss = sol[2] + max(0.0, lim_y[0] - yaw) + max(0.0, yaw - lim_y[1])
            return sol[0], sol[1], miss

        base = attempt(0.0)
        if base is None:
            return {}
        pitch, roll, miss = base
        yaw = 0.0
        if self.shoulder_yaw and miss > 1e-9:
            pitch, roll, yaw = self._yaw_search(side, attempt, base)
            miss = attempt(yaw)[2]
        if self._yaw_prev is None:
            self._yaw_prev = {}
        self._yaw_prev[side] = yaw
        if self._shoulder_miss is None:
            self._shoulder_miss = {}
        self._shoulder_miss[side] = miss
        if miss > 1e-9:
            self.n_shoulder_clamped += 1

        sg = self._WRIST_SIGN[side]
        joints = [(f"{side}_shoulder_pitch_joint", "shoulder_pitch", pitch, lim_p),
                  (f"{side}_shoulder_roll_joint", "shoulder_roll", roll, lim_r)]
        if self.shoulder_yaw:
            joints.append((f"{side}_shoulder_yaw_joint", "shoulder_yaw", yaw, lim_y))
        out = {}
        for name, axis, v_, (lo, hi) in joints:
            out[name] = min(hi, max(lo, self._degap(side, name, v_ * sg[axis])))
        return out

    def _yaw_search(self, side, attempt, base):
        """-> (pitch, roll, yaw): the smallest |yaw| that reaches, or least miss."""
        prev = (self._yaw_prev or {}).get(side, 0.0)
        step = self.YAW_SEARCH_STEP
        n = int(self.YAW_SEARCH_MAX / step + 1e-9)
        cands = []                          # (yaw, pitch, roll, miss)
        for i in range(1, n + 1):
            for sgn in (1.0, -1.0):
                y = sgn * i * step
                sol = attempt(y)
                if sol is not None:
                    cands.append((y, sol[0], sol[1], sol[2]))
        reach = [c for c in cands if c[3] <= 1e-9]
        if not reach:
            y, pitch, roll, _ = min([(0.0, *base)] + cands, key=lambda c: (c[3], abs(c[0])))
            return pitch, roll, y
        # Smallest |yaw| first; between the two signs at the same |yaw|, the
        # one used last frame, so a hand near the midline does not flap.
        reach.sort(key=lambda c: (abs(c[0]), 0 if (c[0] > 0) == (prev > 0) else 1))
        y_hi = reach[0][0]
        # The grid point one step toward zero did not reach (or is zero, which
        # did not). Bisect to the boundary: 10 halvings of 5 deg is 0.005 deg.
        y_lo = y_hi - _math.copysign(step, y_hi)
        for _ in range(10):
            mid = 0.5 * (y_lo + y_hi)
            sol = attempt(mid)
            if sol is not None and sol[2] <= 1e-9:
                y_hi = mid
            else:
                y_lo = mid
        pitch, roll, _ = attempt(y_hi)
        return pitch, roll, y_hi

    def _forearm_q(self, side, hand, shoulder=None):
        """-> {joint_name: angle} for the elbow + three wrist joints of one arm.

        `shoulder` is the pitch/roll this frame is commanding, when the
        shoulder is being driven. Given it, the wrist is solved against the
        forearm's ACTUAL orientation; without it the forearm frame is constant
        and the cheaper conjugation below is exact.
        """
        import math as _m
        out = {}
        pre = f"{side}_"
        R_w = _quat_to_R(hand["quat"])
        if hand.get("source") == "controller":
            # Bring the controller's grip frame onto the wrist convention
            # everything below assumes. See _GRIP_TO_WRIST.
            R_w = R_w @ self._GRIP_TO_WRIST[side]
        # RELATIVE to the pose the operator zeroed at, not absolute.
        #
        # `quat` is the wrist's orientation in the head's HEADING frame
        # (web/teleop.js strips head pitch and roll and keeps yaw), so turning
        # the body is already cancelled before it gets here. What is left is an
        # OFFSET: the operator's comfortable neutral hand orientation has no
        # reason to match the robot's held wrist angles, and read absolutely
        # that difference is a standing command -- arm the robot and the wrists
        # snap somewhere arbitrary and sit there.
        #
        # Referencing to a captured pose removes it. R_0^T @ R_w is the change
        # in wrist orientation since zeroing, which is what the robot's wrist
        # joints should reproduce; C then carries it from the XR wrist frame
        # into the robot's hand frame.
        #
        # What this does NOT do is separate wrist bend from forearm rotation.
        # WebXR reports 25 hand joints and no elbow, so if the operator rotates
        # their whole forearm the wrist follows. With the shoulder held that is
        # close enough to be useful, and it is the reason the operator is told
        # to keep their upper arm still.
        C = self._WRIST_ROT[side]
        # The apparent left/right asymmetry that kept this off was a TEST
        # artifact, not a bug: the comparison fed both hands the SAME hand
        # orientation, and a real operator's two hands at one posture are
        # mirror images. Feed mirrored poses and whichever arm can reach it
        # gives 0.0000 deg while the other saturates its wrist limits -- flip
        # the mirroring and the "broken" side swaps. Checks that do hold:
        # _shoulder_q, forearm_orientation and the composed wrist path are all
        # exactly mirrored, verified pose by pose.
        if shoulder is None or not self.wrist_fk:
            # Shoulder held: the forearm frame does not move, so conjugating
            # the operator's change in hand orientation by a constant is exact.
            # This is the path verified on hardware, and it stays untouched.
            R_rel = self._wrist_zero(side, R_w).T @ R_w
            R = C @ R_rel @ C.T
        else:
            # Shoulder driven: the forearm frame rotates with it, so a constant
            # conjugation is wrong by the whole shoulder motion -- up to 108 deg
            # of roll on a recorded session, seen by the operator as the hand
            # tilting whenever they raise the arm. Solve in the frame the wrist
            # joints actually act in, and RELATIVE to the zero pose, exactly as
            # the verified path is:
            #
            #   change in hand orientation since zero, in the robot's hand
            #   frame (the verified quantity)         : H = C @ R_0^T R_w @ C^T
            #   robot hand at zero (wrist joints = 0,
            #   so it IS the forearm at zero)         : F_0
            #   the hand the robot should have now    : F_0 @ H
            #   forearm under the commanded shoulder  : F(pitch, roll, elbow)
            #   the wrist joints                      : F^T @ F_0 @ H
            #
            # With the shoulder held F == F_0 and this is the constant path to
            # the last bit, so it cannot drift from the mapping verified to
            # +0.99 on hardware. With the shoulder moving, only F changes.
            #
            # An earlier version used the ABSOLUTE hand orientation,
            # F^T (A R_w C^T), and measured as an improvement on the left arm
            # and a regression on the right (74.3 -> 116.5 deg of tilt). That
            # was read as a mirroring error in F or C. It was neither: F
            # matches MuJoCo FK to 0.0001 deg on both arms and C_right is the
            # exact mirror of C_left. Read absolutely, the operator's neutral
            # hand sat 108-163 deg of roll from the robot's zero, the wrist
            # clamp bit on one side of one test pose, and the poses were not
            # mirrored. Zero-relative, the residual is zero on both sides.
            #
            # Known approximation: F is built from the COMMANDED shoulder, and
            # after arming the sink subtracts a constant so the robot sits
            # commanded-minus-offset. Only F^T F_0 matters and both carry the
            # same offset, so the error is second order in offset x motion.
            # Zeroing right before arming, which the runbook already asks for,
            # keeps the offset small.
            e0 = self._elbow_q(side, hand)
            if e0 is None:
                return {}
            yaw = shoulder[2] if len(shoulder) > 2 else 0.0
            F = forearm_orientation(side, shoulder[0], shoulder[1], float(e0), yaw)
            R_0, F_0 = self._forearm_zero(side, R_w, F)
            R = F.T @ F_0 @ (C @ R_0.T @ R_w @ C.T)
        # XYZ euler, because that is the chain the robot actually has:
        # wrist_roll about X, then pitch about Y, then yaw about Z, with zero
        # rpy between them (assets/g1/g1_body29_hand14.urdf). So the composed
        # orientation is Rx(roll)@Ry(pitch)@Rz(yaw), and decomposing it as ZYX
        # -- which an earlier version of this did -- scrambles the axes.
        sb = max(-1.0, min(1.0, R[0, 2]))
        pitch = _m.asin(sb)
        yaw = _m.atan2(-R[0, 1], R[0, 0])
        roll = _m.atan2(-R[1, 2], R[2, 2])
        sg = self._WRIST_SIGN[side]
        for axis, v in (("roll", roll * sg["roll"]),
                        ("pitch", pitch * sg["pitch"]),
                        ("yaw", yaw * sg["yaw"])):
            name = f"{pre}wrist_{axis}_joint"
            # HUMAN_WRIST applies on BOTH paths, and that is deliberate.
            #
            # An earlier fix skipped it on the forearm-FK path, because that
            # path was written ABSOLUTELY and the clamp then bit on every pose
            # (roll pinned at +-84.8 of an +-85 clamp). The real problem was
            # the absolute formulation, not the clamp: solved RELATIVE to the
            # zero pose, as it is now, the values sit near zero and the clamp
            # only bites when the operator really does exceed a human wrist.
            # Skipping it here would let whole-ARM rotation back into the
            # wrist and would break the reduction test below.
            h = self.HUMAN_WRIST[axis]
            v = min(h, max(-h, v))           # human range first...
            v = self._degap(side, axis, v)   # ...then reject impossible steps...
            lo, hi = ARM_JOINT_LIMITS[name]
            out[name] = min(hi, max(lo, v))  # ...then the robot's own limits
        e = self._elbow_q(side, hand)
        if e is not None:
            # Same gate as the wrists. Found by replaying a real session
            # through this code offline: the left elbow's p99 step is
            # 3.3 deg and its max is 40.8, which is the teleport signature
            # again -- here it comes from the wrist POSITION jumping on a
            # tracking dropout rather than from the Euler decomposition, but it
            # reaches the robot the same way and the leash smears it the same
            # way.
            lo, hi = ARM_JOINT_LIMITS[f"{pre}elbow_joint"]
            out[f"{pre}elbow_joint"] = min(hi, max(lo, self._degap(side, "elbow", e)))
        return out

    def cal_start(self):
        self._cal_rec = {"samples": [], "open": None, "closed": None}
        return "recording — hold the pose, then cal_mark_open or cal_mark_closed"

    def cal_mark(self, which):
        """Freeze the median of the recent samples as one extreme."""
        if self._cal_rec is None:
            return "refused: not recording"
        s = self._cal_rec["samples"][-90:]      # ~1 s at 90 fps
        if len(s) < 10:
            return f"refused: only {len(s)} samples — hold the pose and retry"
        med = {}
        for k in DEX3_LEFT + DEX3_RIGHT:
            vals = sorted(r[k] for r in s if k in r)
            if vals:
                med[k] = vals[len(vals) // 2]
        self._cal_rec[which] = med
        self._cal_rec["samples"] = []
        return f"{which} pose captured ({len(s)} samples)"

    def cal_finish(self):
        rec = self._cal_rec
        if rec is None:
            return "refused: not recording"
        if not rec["open"] or not rec["closed"]:
            return "refused: need both an open and a closed pose first"
        cal, weak = {}, []
        for k in DEX3_LEFT + DEX3_RIGHT:
            o, c = rec["open"].get(k), rec["closed"].get(k)
            if o is None or c is None:
                continue
            if abs(c - o) < CAL_MIN_SPAN:
                weak.append(k.replace("_hand_", " ").replace("_joint", ""))
                continue
            cal[k] = (o, c)
        self._cal, self._cal_rec = cal, None
        msg = f"finger calibration on for {len(cal)}/14 joints"
        if weak:
            msg += (f"; {len(weak)} moved less than {CAL_MIN_SPAN} rad between "
                    f"your poses and are left alone: {weak}")
        msg += "; " + self._save_finger_cal()
        return msg

    def _save_finger_cal(self):
        """Write the calibration out. -> a phrase for the finish message.

        It used to live only in memory, and that quietly cost an evening: the
        operator recalibrated their right thumb (usable travel 24 % -> 69 %),
        the next bridge deploy replaced the process, the file on disk was from
        that morning, and the fix was gone. Nobody noticed, because the thing
        it fixed -- grasping a can -- then worked anyway on the old numbers.
        Five minutes of an operator's time should outlive a deploy.

        Written atomically, because a half-written calibration that still
        parses is worse than none: it would be loaded, believed, and would
        drive fingers to the wrong place.

        NOTE: this file lives in the container image, so it survives a
        restart but NOT a redeploy. Durably keeping it means pulling
        GET /fingercal/dump into bridge/calibration/finger_cal.json in the
        repo, which is what ships in the next image.
        """
        try:
            path = self._CAL_FILE
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"joints": {k: list(v) for k, v in (self._cal or {}).items()}},
                          fh, indent=1)
            os.replace(tmp, path)
            return f"saved to {path}"
        except OSError as e:
            return f"NOT saved ({e}) -- pull /fingercal/dump before redeploying"

    def cal_clear(self):
        self._cal, self._cal_rec = None, None
        return "finger calibration cleared"

    def _finger_q(self, landmarks_25x3, indices, retargeting, to_hw, R):
        data = np.asarray(landmarks_25x3, dtype=float).reshape(25, 3)
        ref = data[indices[1, :]] - data[indices[0, :]]
        # Into the robot's hand frame. These are DIFFERENCES between landmarks,
        # so only the rotation matters -- a translation would cancel.
        ref = ref @ R.T
        return retargeting.retarget(ref)[to_hw]

    # Skip the arm IK entirely. Measured on hardware: the Pinocchio/CasADi
    # solve runs at ~10 Hz against 85 fps of hand data, so it, not the link and
    # not the robot, is what limits teleop. Fingers do not need it -- DexPilot
    # runs on wrist-local landmarks.
    #
    # In this mode the arm joints are ABSENT from the output, not zeroed. The
    # sink then sends a frame with no "arm" key at all, so there is no arm data
    # on the wire for anything downstream to command an arm with. Zeroing them
    # would have been one config mistake away from driving both arms to the
    # zero pose -- the exact bug fixed in 211a9da.
    fingers_only = False

    # Forearm mode: elbow + the three wrist joints follow the operator, the
    # SHOULDER is held wherever the robot already is. No IK -- the wrist joints
    # come straight from the operator's wrist orientation, which is what the
    # 95 ms IPOPT solve was being asked to infer indirectly.
    #
    # Only the driven joints go on the wire, by name. The shoulder is absent
    # rather than filled in, so nothing downstream can command it: the same
    # guarantee fingers-only relies on, one joint group up.
    forearm = False

    # Shoulder pitch and roll follow the operator too, analytically. Separate
    # from `forearm` and off by default because it is the first thing that
    # swings the whole arm rather than a wrist: a wrong wrist target moves a
    # hand, a wrong shoulder target moves everything attached to it.
    shoulder = False

    # Solve the wrist against the forearm's actual orientation rather than a
    # constant, relative to the zero pose -- see _forearm_q. Only read when
    # the shoulder is driven; forearm-only mode never reaches it. server.py
    # turns it on with the shoulder (TELEOP_WRIST_FK=0 to run without it),
    # because a moving shoulder under a constant wrist conjugation tilts the
    # hand by the whole shoulder motion.
    #
    # Measured over 200 random poses per side, counting only poses that do not
    # saturate a wrist limit: solving against the forearm gives a hand-
    # orientation error of 0.0000 deg (max, both sides) where the constant
    # conjugation gives a p50 of ~86 deg. It is not an improvement, it is the
    # difference between correct and not.
    #
    # The class default stays False and server.py enables it with the shoulder,
    # so a caller that constructs a retargeter directly gets the conservative
    # path unless it asks.
    wrist_fk = False

    # Finger calibration: map the operator's ACTUAL range onto the joint's.
    #
    # DexPilot's cost matches fingertip-to-thumb vectors, which pins down grasp
    # poses well and leaves full extension underdetermined. Measured on
    # hardware: the curl end hits the joint limit exactly, while a flat open
    # hand only reaches about halfway -- left middle_0 stopped at -0.879 of a
    # [-1.571, 0] range, right index_0 at +0.959 of [0, 1.571]. The fingers
    # never opened past ~50 %.
    #
    # Rather than hope a different cost function lands better, record what this
    # operator's open and closed hands actually produce and stretch it linearly
    # onto the joint range. It also adapts to hand size, which a fixed mapping
    # cannot.
    _cal = None          # {joint: (open_value, closed_value)}
    # A calibration shipped with the image, so a redeploy does not throw away a
    # session's worth of posing. Same reasoning as the arm calibration in
    # real/calibration/: measured once, versioned, reloadable.
    _CAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "calibration", "finger_cal.json")
    _cal_rec = None      # {"open"|"closed": {joint: value}} while recording

    # Set by __init__ when TELEOP_ASYNC_IK is on; None means solve inline.
    _ik_worker = None

    # ---- output smoothing -------------------------------------------------
    #
    # One exponential filter on the ARM targets, time constant in seconds.
    # Fingers are deliberately excluded: a grasp that responds late feels
    # broken in a way a slightly-late elbow does not, and the whole reason the
    # arm IK was dropped was to keep the fingers running at headset rate.
    #
    # Off by default (0.0), so a caller that does not ask gets what it always
    # got. Unlike the joint gains (which need HOLDING) or the onboard slew
    # (which needs a robot deploy), this can be dialled while the robot is
    # running -- set_smooth_tau() -- so it can be tuned by feel.
    smooth_tau = float(os.environ.get("TELEOP_SMOOTH_TAU", "0") or 0)
    _smooth = None            # {joint: filtered value}
    _smooth_t = None          # monotonic stamp of the last filtered frame

    _FINGER_NAMES = frozenset(DEX3_LEFT + DEX3_RIGHT)

    def set_smooth_tau(self, tau):
        """Change the smoothing time constant, in seconds. 0 turns it off."""
        tau = float(tau)
        if not _math.isfinite(tau) or tau < 0.0:
            raise ValueError("tau must be a finite, non-negative number of seconds")
        self.smooth_tau = min(tau, 1.0)     # past ~1 s it is no longer teleop
        if self.smooth_tau == 0.0:
            self._smooth, self._smooth_t = None, None
        return self.smooth_tau

    # Joints pinned to their zero-pose value, by name. The WRIST joints are
    # solved relative to the pose the operator zeroed at, so 0.0 for one of
    # them means "exactly where this joint was when you armed" -- a frozen
    # joint, not a joint driven to some absolute angle. That is what makes
    # this safe to do live: it can only ever command the pose the arm is
    # already holding for that joint.
    #
    # Asked for by the operator while holding a can: a wrist that pitches is a
    # can that pours, and the hand does not need that axis to grasp.
    frozen = frozenset()

    def set_frozen(self, names):
        """Pin these joints to their zero-pose value. Empty clears."""
        if isinstance(names, str):
            names = [n.strip() for n in names.split(",") if n.strip()]
        names = frozenset(names or ())
        bad = [n for n in names if n not in ARM_JOINT_LIMITS]
        if bad:
            raise ValueError(f"not arm joints: {', '.join(sorted(bad))}")
        self.frozen = names
        return sorted(names)

    # "Smoother AND less laggy", which a fixed low-pass cannot do: its
    # smoothness IS its lag. The 1-euro filter (Casiez, Roussel & Vogel, CHI
    # 2012) buys both by making the cutoff depend on speed -- heavy smoothing
    # when the hand is nearly still, where jitter is what you see, and almost
    # none when it moves fast, where lag is what you feel. It is the standard
    # answer for hand tracking and it is two first-order filters.
    #
    # min_cutoff Hz sets the steadiness of a held pose; beta sets how quickly
    # the filter gets out of the way once moving. Both live-tunable.
    # OFF by default, like the fixed filter it joins. Turning a filter on by
    # default would change every caller's output silently, including the
    # replay tests that reproduce a recorded session -- and "without the
    # switch nothing changes" is the rule the arm-mode switch already follows.
    # server.py turns it on (TELEOP_SMOOTH_MODE).
    smooth_mode = "off"            # "oneeuro" | "ema" | "off"
    smooth_min_cutoff = 1.0        # Hz
    smooth_beta = 1.0
    # Relative cost of moving each part of the arm. Greater than one lowers
    # the One Euro cutoff (heavier/slower); less than one raises it
    # (lighter/more responsive). This preserves the microsecond geometry path
    # instead of switching to the 95-200 ms optimization IK.
    smooth_joint_cost = {"shoulder": 1.0, "elbow": 1.0, "wrist": 1.0}
    _euro = None                   # {joint: (x_hat, dx_hat)}

    def set_smooth(self, mode=None, min_cutoff=None, beta=None):
        if mode is not None:
            mode = str(mode).lower()
            if mode not in ("oneeuro", "ema", "off"):
                raise ValueError("mode must be oneeuro, ema or off")
            self.smooth_mode = mode
        for name, v, lo, hi in (("smooth_min_cutoff", min_cutoff, 0.01, 20.0),
                                ("smooth_beta", beta, 0.0, 20.0)):
            if v is None:
                continue
            v = float(v)
            if not _math.isfinite(v) or not lo <= v <= hi:
                raise ValueError(f"{name} must be finite in [{lo}, {hi}]")
            setattr(self, name, v)
        self._euro, self._smooth, self._smooth_t = None, None, None
        return {"mode": self.smooth_mode, "min_cutoff": self.smooth_min_cutoff,
                "beta": self.smooth_beta, "tau": self.smooth_tau}

    def set_smooth_cost(self, shoulder=None, elbow=None, wrist=None):
        costs = dict(getattr(self, "smooth_joint_cost", {}) or {})
        for name, value in (("shoulder", shoulder), ("elbow", elbow), ("wrist", wrist)):
            if value is None:
                costs.setdefault(name, 1.0)
                continue
            value = float(value)
            if not _math.isfinite(value) or not 0.1 <= value <= 10.0:
                raise ValueError(f"{name} motion cost must be finite in [0.1, 10.0]")
            costs[name] = value
        self.smooth_joint_cost = costs
        self._euro, self._smooth, self._smooth_t = None, None, None
        return dict(costs)

    def _smooth_cost(self, joint):
        if "shoulder" in joint:
            kind = "shoulder"
        elif "elbow" in joint:
            kind = "elbow"
        else:
            kind = "wrist"
        return float((getattr(self, "smooth_joint_cost", None) or {}).get(kind, 1.0))

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2.0 * _math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def _one_euro(self, out, dt):
        """Speed-adaptive low-pass. Slow hand -> smooth; fast hand -> responsive."""
        if self._euro is None:
            self._euro = {}
        for k, v in out.items():
            if k in self._FINGER_NAMES:
                continue
            st = self._euro.get(k)
            if st is None:
                self._euro[k] = (v, 0.0)
                continue
            x_prev, dx_prev = st
            if dt <= 0.0:
                out[k] = x_prev
                continue
            dx = (v - x_prev) / dt
            # Derivative filtered at a fixed 1 Hz, as the paper has it: the
            # speed estimate must not itself be noisy or the cutoff chatters.
            dx_hat = dx_prev + self._alpha(1.0, dt) * (dx - dx_prev)
            # Cost changes response speed, not the geometric target: the elbow
            # can be made steadier while the shoulder gets out of the way.
            cutoff = ((self.smooth_min_cutoff + self.smooth_beta * abs(dx_hat))
                      / self._smooth_cost(k))
            x_hat = x_prev + self._alpha(cutoff, dt) * (v - x_prev)
            self._euro[k] = (x_hat, dx_hat)
            out[k] = x_hat
        return out

    # How much of the operator's wrist motion reaches the robot, per axis.
    #
    # The wrist joints are solved RELATIVE to the zero pose, so scaling them
    # scales the motion away from that pose: 0.5 means the robot's wrist moves
    # half as far as the operator's, everywhere, and 0.0 is a freeze.
    #
    # Asked for because wrist PITCH "gets into unnatural positions too easily".
    # A scale, not a tighter clamp: a clamp leaves the axis just as twitchy
    # until it hits a wall, while this makes fine control easier across the
    # whole range, which is what "too easily" is actually about.
    wrist_scale = {"roll": 1.0, "pitch": 1.0, "yaw": 1.0}

    def set_yaw_max(self, degrees):
        """Cap the shoulder-yaw search, in degrees. 0 turns the search off."""
        v = float(degrees)
        if not _math.isfinite(v) or not 0.0 <= v <= 90.0:
            raise ValueError("yaw_max must be a finite number of degrees in [0, 90]")
        self.YAW_SEARCH_MAX = v / 57.29577951308232
        # At 0 the grid has no points and `attempt(0.0)` is all there is, which
        # is the two-joint shoulder exactly -- so say so rather than leaving a
        # search that cannot search.
        self.shoulder_yaw = v > 0.0
        return round(v, 2)

    def set_wrist_scale(self, **axes):
        """roll=/pitch=/yaw= in [0, 1]. Returns the whole table."""
        scale = dict(self.wrist_scale)
        for k, v in axes.items():
            if v is None:
                continue
            if k not in ("roll", "pitch", "yaw"):
                raise ValueError(f"unknown wrist axis {k!r}")
            v = float(v)
            if not _math.isfinite(v) or not 0.0 <= v <= 1.0:
                raise ValueError(f"{k} must be a finite number in [0, 1]")
            scale[k] = v
        self.wrist_scale = scale
        return dict(scale)

    def _smoothed(self, out):
        """Freeze what was asked for, then filter the arm targets, in place.

        One post-processing hook rather than two: every return point in
        process() already goes through here, and a second one is a second
        place to forget.
        """
        for n in self.frozen:
            if n in out:
                out[n] = 0.0
        # Scale BEFORE the filter, for the same reason freeze is before it:
        # the filter must smooth what is actually being commanded, not chase a
        # target that is then scaled out from under it.
        sc = self.wrist_scale
        if sc and any(v != 1.0 for v in sc.values()):
            for n in list(out):
                if "_wrist_" not in n:
                    continue
                axis = n.split("_wrist_", 1)[1].rsplit("_joint", 1)[0]
                f = sc.get(axis)
                if f is not None and f != 1.0:
                    out[n] = out[n] * f
        if self.smooth_mode == "oneeuro":
            now = time.monotonic()
            dt = 0.0 if self._smooth_t is None else min(0.2, max(0.0, now - self._smooth_t))
            self._smooth_t = now
            # Seed on the first frame too. Returning raw output at dt=0 left
            # the filter empty and let the SECOND frame's entire step through.
            # Equal timestamps must hold the last filtered value, not bypass.
            return self._one_euro(out, dt)
        if self.smooth_mode == "off":
            return out
        tau = self.smooth_tau
        if tau <= 0.0:
            return out
        now = time.monotonic()
        # dt is measured, not assumed. Tracking makes the frame rate wander,
        # and a filter tuned at 90 fps that silently changed character at 40
        # would be worse than no filter. Capped so that one frame after a long
        # dropout cannot carry most of the way to the new target.
        dt = 0.0 if self._smooth_t is None else min(0.2, max(0.0, now - self._smooth_t))
        self._smooth_t = now
        if self._smooth is None:
            self._smooth = {}
        a = 1.0 - _math.exp(-dt / tau) if dt > 0.0 else 0.0
        for k, v in out.items():
            if k in self._FINGER_NAMES:
                continue
            prev = self._smooth.get(k)
            if prev is None:
                self._smooth[k] = v        # first sight of a joint passes through
            else:
                self._smooth[k] = prev + a * (v - prev)
                out[k] = self._smooth[k]
        return out

    # What drove each hand on the last frame: "hand" (tracked landmarks) or
    # "controller" (grip pose + trigger). For the status page, and so a mixed
    # frame -- one hand tracked, the other holding a controller -- is visible
    # as what it is rather than as one hand missing.
    input_sources = None

    @staticmethod
    def _only_present_fingers(out, hands):
        """Drop the finger joints of any hand that is not in this frame.

        `out` starts from the last frame's targets so a hand that drops out
        of tracking holds still on the wire. For the ARM that is right, and
        the sink refuses partial sets so nothing invents an arm. For the
        FINGERS it wrote a lie into every recording: 3226 rows of one can
        session carried finger targets for a hand that was not there, copied
        unmarked from the last frame it was. A policy trained on that learns
        that a hand it cannot see keeps commanding its last grip. Emit fingers
        only for hands present; the sink holds the last grip for a missing
        hand (bridge/sink_realg1.py), so the robot's behaviour is unchanged
        and the recording is truthful.
        """
        absent = [n for s, names in (("left", DEX3_LEFT), ("right", DEX3_RIGHT))
                  if s not in hands for n in names]
        if not absent:
            return out
        return {k: v for k, v in out.items() if k not in absent}

    def process(self, frame: dict) -> dict:
        hands = {h.get("hand"): h for h in frame.get("hands", [])}
        out = dict(self._last)
        self.input_sources = {s: (h.get("source") or "hand") for s, h in hands.items()}

        # ---- fingers ----
        # Per hand, from whichever the headset sent: 25 tracked landmarks
        # through the retargeter, or a controller's trigger through grip_q.
        # A controller entry carries no landmarks (null), and a tracked hand
        # can arrive with an empty list; neither is a hand to retarget.
        trigger_sides = set()
        for side, names, idx, rt, to_hw, rot in (
                ("left", DEX3_LEFT, "_li", "_left", "_l_to_hw", "_rot_l"),
                ("right", DEX3_RIGHT, "_ri", "_right", "_r_to_hw", "_rot_r")):
            h = hands.get(side)
            if not h:
                continue
            if h.get("source") == "controller":
                slew = getattr(self, "_trigger_grip", None)
                if slew is None:
                    slew = self._trigger_grip = TriggerGripSlew.from_env()
                grip = slew.step(side, h.get("trigger", 0.0))
                if grip is None:
                    for name in names:
                        out.pop(name, None)
                else:
                    out.update(dict(zip(names, grip_q(side, grip))))
                    trigger_sides.add(side)
            elif h.get("landmarks"):
                q = self._finger_q(h["landmarks"], getattr(self, idx), getattr(self, rt),
                                   getattr(self, to_hw), getattr(self, rot))
                out.update(dict(zip(names, q)))

        # Record or apply the finger calibration before anything downstream.
        # The calibration maps LANDMARK-derived angles onto the operator's
        # open/closed; a trigger already spans the joint's own range, so
        # applying it there would double-map.
        if self._cal_rec is not None:
            self._cal_rec["samples"].append(
                {k: out[k] for k in DEX3_LEFT + DEX3_RIGHT if k in out})
        elif self._cal:
            skip = {n for s in trigger_sides for n in (DEX3_LEFT if s == "left" else DEX3_RIGHT)}
            for k, (o, c) in self._cal.items():
                if k in out and k not in skip:
                    out[k] = self._apply_cal(k, out[k], o, c)

        if self.forearm:
            fr = {}
            for side, prefix in (("left", "left"), ("right", "right")):
                h = hands.get(side)
                if not h or "quat" not in h:
                    continue
                # Shoulder FIRST when it is driven: the wrist must be solved
                # against the forearm orientation these angles produce, not
                # against a frame that assumes the shoulder never moved.
                sh = self._shoulder_q(side, h) if self.shoulder else {}
                pr = ((sh[f"{side}_shoulder_pitch_joint"],
                       sh[f"{side}_shoulder_roll_joint"],
                       sh.get(f"{side}_shoulder_yaw_joint", 0.0)) if sh else None)
                fr.update(self._forearm_q(side, h, shoulder=pr))
                fr.update(sh)
            out.update(fr)
            self._last = out
            keep = set(DEX3_LEFT + DEX3_RIGHT) | set(fr)
            return self._smoothed(self._only_present_fingers(
                {k: v for k, v in out.items() if k in keep}, hands))

        if self.fingers_only:
            # Solve the arm IK ONCE, then never again: the solve costs ~95 ms
            # (measured, ~10 Hz against 85 fps of hand data) and the fingers do
            # not need it. After the first solve `out` already carries those
            # arm values via dict(self._last), so the frame stays complete and
            # the arm targets simply stop changing.
            #
            # Emitting them rather than omitting them is a concession to the
            # robot currently deployed, which cannot parse an arm-less frame
            # and has no build host available to update it. It is safe for a
            # different reason, and a stronger one: in `take_hands` mode the
            # onboard never constructs an arm controller at all, so there is
            # nothing on that side able to act on an arm target.
            if all(n in out for n in ARM_LEFT + ARM_RIGHT):
                self._last = out
                return self._smoothed(self._only_present_fingers(dict(out), hands))

        # ---- arms ----
        #
        # Needs BOTH wrists: the IK solves the two arms together, so one hand
        # out of tracking means no new solve rather than half a solve.
        lw = hands.get("left", {})
        rw = hands.get("right", {})
        if "wrist" in lw and "wrist" in rw:
            Tl = _wrist_se3(lw["wrist"], lw.get("quat", [0, 0, 0, 1]))
            Tr = _wrist_se3(rw["wrist"], rw.get("quat", [0, 0, 0, 1]))
            if self._ik_worker is not None:
                # Hand it off and carry on. The worker is always solving the
                # newest target, so this frame gets whatever it finished last
                # -- which is the point: the fingers below run at headset rate
                # instead of at IK rate.
                self._ik_worker.submit(Tl, Tr)
                sol_q, _ = self._ik_worker.latest()
            else:
                sol_q, _ = self._arm_ik.solve_ik(Tl, Tr, self._arm_q)
            self._arm_q = sol_q
            out.update(dict(zip(ARM_LEFT + ARM_RIGHT, sol_q)))
        elif self._ik_worker is not None:
            # No new target, but a solve begun earlier may have landed since.
            sol_q, _ = self._ik_worker.latest()
            self._arm_q = sol_q
            out.update(dict(zip(ARM_LEFT + ARM_RIGHT, sol_q)))

        self._last = out
        return self._smoothed(self._only_present_fingers(dict(out), hands))
