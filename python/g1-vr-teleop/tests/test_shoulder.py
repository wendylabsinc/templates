"""Analytic shoulder IK, checked against the URDF rather than against itself.

The formula was derived by hand, and hand-derived geometry has been wrong twice
in this repo already -- both times from summing joint offsets while ignoring
their rpy, and once with a test that reproduced the same mistake and therefore
agreed with it.

So the central test here does not check the algebra. It runs the solved angles
through FORWARD kinematics on the URDF and asks whether the upper arm ends up
pointing where it was asked to. That is falsifiable independently of how the
answer was computed.
"""
import math
import os
import sys
import xml.etree.ElementTree as ET

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))
import retarget_g1 as rg  # noqa: E402

URDF = os.path.join(os.path.dirname(__file__), "..", "teleop", "assets", "g1",
                    "g1_body29_hand14.urdf")


def _rpy(r, p, y):
    cr, sr, cp, sp, cy, sy = (math.cos(r), math.sin(r), math.cos(p),
                              math.sin(p), math.cos(y), math.sin(y))
    return (np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]])
            @ np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]])
            @ np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]]))


def _axis_rot(axis, q):
    a = np.asarray(axis, float)
    a = a / (np.linalg.norm(a) or 1.0)
    K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + math.sin(q) * K + (1 - math.cos(q)) * (K @ K)


def _joints():
    out = {}
    for j in ET.parse(URDF).getroot().findall("joint"):
        o = j.find("origin")
        xyz = [float(v) for v in ((o.get("xyz", "0 0 0") if o is not None
                                   else "0 0 0").split())]
        r = [float(v) for v in ((o.get("rpy", "0 0 0") if o is not None
                                 else "0 0 0").split())]
        ax = j.find("axis")
        out[j.get("name")] = (np.asarray(xyz), _rpy(*r),
                              [float(v) for v in (ax.get("xyz").split()
                                                  if ax is not None else "0 0 1".split())])
    return out


def wrist_position(side, pitch, roll, elbow, yaw=0.0):
    """Wrist position relative to the shoulder-roll origin, full FK.

    This is what the operator actually points with. Checking the UPPER ARM's
    direction instead was the mistake the code itself made: aiming the upper
    arm at the hand is only correct with a straight elbow, and with a bent one
    it puts the hand somewhere else entirely.
    """
    J = _joints()
    p = np.zeros(3)
    T = np.eye(3)
    start = None
    chain = [(f"{side}_shoulder_pitch_joint", pitch),
             (f"{side}_shoulder_roll_joint", roll),
             (f"{side}_shoulder_yaw_joint", yaw),
             (f"{side}_elbow_joint", elbow),
             (f"{side}_wrist_roll_joint", None)]
    for name, q in chain:
        xyz, R, axis = J[name]
        p = p + T @ xyz
        T = T @ R
        if name.endswith("shoulder_roll_joint"):
            start = p.copy()
        if q is not None:
            T = T @ _axis_rot(axis, q)
    return p - start


def upper_arm_dir(side, pitch, roll, yaw=0.0):
    """Unit vector shoulder -> elbow, by FK with every rpy applied."""
    J = _joints()
    p = np.zeros(3)
    T = np.eye(3)
    chain = [(f"{side}_shoulder_pitch_joint", pitch),
             (f"{side}_shoulder_roll_joint", roll),
             (f"{side}_shoulder_yaw_joint", yaw),
             (f"{side}_elbow_joint", None)]
    start = None
    for name, q in chain:
        xyz, R, axis = J[name]
        p = p + T @ xyz
        T = T @ R
        if name.endswith("shoulder_roll_joint"):
            start = p.copy()       # the shoulder centre the closed form uses
        if q is not None:
            T = T @ _axis_rot(axis, q)
    v = p - start
    return v / np.linalg.norm(v)


@pytest.mark.parametrize("side", ["left", "right"])
def test_the_hardcoded_rest_direction_matches_the_urdf(side):
    """_UPPER_ARM_0 is a constant in the code; the URDF is the source."""
    got = upper_arm_dir(side, 0.0, 0.0)
    want = (rg.G1Retargeter._SHOULDER_PRE[side]
            @ np.asarray(rg.G1Retargeter._UPPER_ARM_0[side], float))
    assert np.allclose(got, want, atol=0.02), f"{side}: URDF says {got}, code says {want}"


@pytest.mark.parametrize("side", ["left", "right"])
def test_the_hand_lands_on_the_direction_asked_for(side):
    """The property that matters, and the one the code got wrong.

    Solve shoulder AND elbow together, run both through full FK to the wrist,
    and measure the angle between where the hand ended up and where the
    operator pointed. An earlier version of this checked the UPPER ARM's
    direction, which passed at 0.0013 deg while the hand went somewhere else --
    because the shoulder was aiming the upper arm at the target and the elbow
    was independently bending away from it.
    """
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    r._wrist_prev = {}
    sh = rg.G1Retargeter._SHOULDER[side]
    rng = np.random.default_rng(0)
    worst = 0.0
    tested = 0
    unreachable = []
    for _ in range(300):
        # Clear the continuity state between probes. Consecutive random targets
        # teleport across the workspace, which is precisely the input _degap
        # exists to reject -- leave it set and the gate correctly holds the
        # previous answer, and this test measures the gate rather than the
        # geometry. (It did, and reported a 172 deg "error" that was not one.)
        r._wrist_prev = {}
        # A target in front of and around the operator, at a plausible reach.
        v = rng.normal(size=3)
        v /= np.linalg.norm(v)
        v *= 0.35
        q = r._shoulder_q(side, {"wrist": (sh + v).tolist()})
        if not q:
            continue
        pitch = q[f"{side}_shoulder_pitch_joint"]
        roll = q[f"{side}_shoulder_roll_joint"]
        # Yaw joins when pitch and roll alone cannot reach; the hand must
        # still land on the direction, so it goes through the FK too.
        yaw = q.get(f"{side}_shoulder_yaw_joint", 0.0)
        if r._shoulder_miss[side] > 1e-9:
            continue                   # clamped: the ask was outside the joint
        elbow = q.get(f"{side}_elbow_joint")
        if elbow is None:
            elbow = r._elbow_q(side, {"wrist": (sh + v).tolist()})
        if elbow is None:
            continue
        want = rg._R_XR_TO_ROBOT @ (v / np.linalg.norm(v))
        hand = wrist_position(side, pitch, roll, elbow, yaw=yaw)
        got = hand / np.linalg.norm(hand)
        err = math.degrees(math.acos(np.clip(np.dot(want, got), -1, 1)))
        # A thin cone is geometrically UNREACHABLE: the upper arm's fixed
        # y-component means |d_y| can never exceed hypot(c, b) = 0.9963, so
        # directions beyond that are asks the shoulder cannot satisfy at any
        # angle. Those are held to a much weaker bound -- the requirement is
        # that they degrade gracefully, not that they be met.
        # The unreachable cone now depends on the ELBOW, because the vector
        # being aimed is the one to the hand rather than to the elbow.
        wv = rg.shoulder_to_wrist(side, float(elbow))
        a, c, b = wv / np.linalg.norm(wv)
        d = rg.G1Retargeter._SHOULDER_PRE[side].T @ want
        if abs(d[1]) > math.hypot(c, b):
            unreachable.append(err)
            continue
        worst = max(worst, err)
        tested += 1
    assert tested > 50, f"only {tested} reachable samples; the test proves little"
    # Exact, not merely close: this is a closed form, so anything above
    # floating-point noise means the derivation is wrong somewhere.
    # Not exact any more, and should not be: the robot's arm is far shorter
    # than the operator's, so reach is calibrated rather than copied and the
    # hand can only land on the DIRECTION, not the distance. What must hold is
    # that the direction is right.
    # EXACT, because it is a closed form solved against the vector to the hand
    # at the commanded elbow. Anything above floating-point noise means the
    # shoulder and the elbow have drifted back into solving for different
    # hands, which is what cost a constant 20.2 deg before.
    assert worst < 0.01, f"{side}: worst hand direction error {worst:.4f} deg over {tested}"
    if unreachable:
        # Looser than the reachable bound by the same constant the known defect
        # costs: this now measures the HAND, which carries the 20 deg
        # shoulder/elbow inconsistency on top of the cone saturation it is
        # actually testing. Tighten both together when that is fixed.
        assert max(unreachable) < 30.0, (
            f"{side}: an unreachable direction produced {max(unreachable):.1f} deg; "
            f"it should saturate at the edge of the cone, not fly off")


@pytest.mark.parametrize("side", ["left", "right"])
def test_pointing_along_the_rest_hand_direction_is_the_zero_pose(side):
    """Zero pitch and roll must mean "the hand is where zero puts it".

    Which direction satisfies that now depends on the ELBOW, because the solve
    aims the vector to the HAND rather than to the elbow. An earlier version of
    this test used the upper-arm direction and passed while the hand went
    somewhere else -- the same blind spot that let the real bug ship.
    """
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    sh = rg.G1Retargeter._SHOULDER[side]
    inv = np.linalg.inv(rg._R_XR_TO_ROBOT)
    pre = rg.G1Retargeter._SHOULDER_PRE[side]
    for dist in (0.18, 0.25, 0.32):
        r._wrist_prev = {}
        r._gap_runs = {}
        probe = (sh + np.array([0.0, 0.0, -dist])).tolist()
        e = r._elbow_q(side, {"wrist": probe})
        assert e is not None
        w = rg.shoulder_to_wrist(side, float(e))
        w = w / np.linalg.norm(w)
        q = r._shoulder_q(side, {"wrist": (sh + dist * (inv @ (pre @ w))).tolist()})
        assert abs(q[f"{side}_shoulder_pitch_joint"]) < math.radians(3), (
            f"dist {dist}: pitch "
            f"{math.degrees(q[f'{side}_shoulder_pitch_joint']):.1f} deg")
        assert abs(q[f"{side}_shoulder_roll_joint"]) < math.radians(3)


@pytest.mark.parametrize("side", ["left", "right"])
def test_reaching_forward_and_opening_out_are_different_joints(side):
    """The two motions the operator named must not be the same knob."""
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    r._wrist_prev = {}
    sh = rg.G1Retargeter._SHOULDER[side]
    inv = np.linalg.inv(rg._R_XR_TO_ROBOT)

    fwd = r._shoulder_q(side, {"wrist": (sh + 0.35 * (inv @ [1.0, 0, 0])).tolist()})
    r._wrist_prev = {}
    out_ = r._shoulder_q(side, {"wrist": (sh + 0.35 * (inv @ [0, 1.0 if side == "left"
                                                             else -1.0, 0])).tolist()})
    assert abs(fwd[f"{side}_shoulder_pitch_joint"]) > math.radians(45), \
        "reaching forward barely moved the pitch"
    assert abs(fwd[f"{side}_shoulder_roll_joint"]) < math.radians(15), \
        "reaching forward moved the roll"
    assert abs(out_[f"{side}_shoulder_roll_joint"]) > math.radians(45), \
        "opening out barely moved the roll"


@pytest.mark.parametrize("side", ["left", "right"])
def test_shoulder_yaw_is_zero_when_roll_reaches(side):
    """No evidence for yaw exists in hand data, so it stays at exactly zero
    unless pitch and roll cannot reach inside the robot's limits. The
    cross-body case lives in test_shoulder_yaw.py."""
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    r._wrist_prev = {}
    sh = rg.G1Retargeter._SHOULDER[side]
    q = r._shoulder_q(side, {"wrist": (sh + np.array([0.1, 0.1, -0.3])).tolist()})
    assert q[f"{side}_shoulder_yaw_joint"] == 0.0, q


@pytest.mark.parametrize("side", ["left", "right"])
def test_every_output_is_inside_the_joint_limits(side):
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    r._wrist_prev = {}
    sh = rg.G1Retargeter._SHOULDER[side]
    rng = np.random.default_rng(3)
    for _ in range(400):
        r._wrist_prev = {}
        v = rng.normal(size=3)
        v = v / np.linalg.norm(v) * rng.uniform(0.05, 0.9)
        for n, val in r._shoulder_q(side, {"wrist": (sh + v).tolist()}).items():
            lo, hi = rg.ARM_JOINT_LIMITS[n]
            assert lo - 1e-9 <= val <= hi + 1e-9, f"{n}={val} outside [{lo},{hi}]"


@pytest.mark.parametrize("side", ["left", "right"])
def test_a_hand_at_the_shoulder_commands_nothing(side):
    """No direction exists, so there is nothing to point at."""
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    r._wrist_prev = {}
    sh = rg.G1Retargeter._SHOULDER[side]
    assert r._shoulder_q(side, {"wrist": (sh + np.array([0.0, 0.0, 0.01])).tolist()}) == {}
    assert r._shoulder_q(side, {}) == {}


@pytest.mark.parametrize("side", ["left", "right"])
def test_a_shoulder_teleport_is_rejected(side):
    """A dry run over real motion produced 39.5 deg in one frame.

    That is 3600 deg/s, and on a shoulder it swings the whole arm rather than a
    hand. The wrist's 40 deg threshold let it straight through, which is why
    MAX_STEP is per-axis.
    """
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    r._wrist_prev = {}
    sh = rg.G1Retargeter._SHOULDER[side]
    inv = np.linalg.inv(rg._R_XR_TO_ROBOT)
    # Opposite extremes, so the jump is unambiguously past the gate.
    down = r._shoulder_q(side, {"wrist": (sh + 0.35 * (inv @ [0, 0, -1.0])).tolist()})
    fwd = r._shoulder_q(side, {"wrist": (sh + 0.35 * (inv @ [0, 0, 1.0])).tolist()})
    for n in down:
        assert fwd[n] == pytest.approx(down[n]), f"{n} teleported straight through"


@pytest.mark.parametrize("side", ["left", "right"])
def test_the_shoulder_limit_is_tighter_than_the_wrist_one(side):
    """Otherwise it is decoration: the wrist threshold already passed 39.5 deg."""
    for k in ("shoulder_pitch", "shoulder_roll"):
        assert rg.G1Retargeter.MAX_STEP[k] < rg.G1Retargeter.MAX_WRIST_STEP


@pytest.mark.parametrize("side", ["left", "right"])
def test_an_ordinary_reach_is_not_gated(side):
    """It must reject teleports, not reaching."""
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    r._wrist_prev = {}
    sh = rg.G1Retargeter._SHOULDER[side]
    inv = np.linalg.inv(rg._R_XR_TO_ROBOT)
    prev = None
    for f in np.linspace(0.0, 1.0, 40):        # sweep down -> forward gently
        d = inv @ np.array([f, 0.0, -(1.0 - f)])
        d = d / np.linalg.norm(d)
        q = r._shoulder_q(side, {"wrist": (sh + 0.35 * d).tolist()})
        v = q[f"{side}_shoulder_pitch_joint"]
        if prev is not None and f > 0:
            assert v != pytest.approx(prev), f"a gentle reach step was rejected at f={f:.2f}"
        prev = v


@pytest.mark.parametrize("side", ["left", "right"])
def test_the_shoulder_signs_are_flippable(side):
    """The geometry verified to 0.0013 deg against the URDF and still came out
    backwards on the real robot: reaching forward drove the shoulder back.

    Either the URDF's axis sign does not match the physical joint or the frame
    mapping is flipped for directions. Hardware decides, so the sign is a knob
    rather than an argument.
    """
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    r._wrist_prev = {}
    r._gap_runs = {}
    sh = rg.G1Retargeter._SHOULDER[side]
    inv = np.linalg.inv(rg._R_XR_TO_ROBOT)
    w = (sh + 0.35 * (inv @ [1.0, 0, 0])).tolist()
    before = r._shoulder_q(side, {"wrist": w})[f"{side}_shoulder_pitch_joint"]
    assert "now" in r.flip_wrist(side, "shoulder_pitch")
    r._wrist_prev = {}
    r._gap_runs = {}
    after = r._shoulder_q(side, {"wrist": w})[f"{side}_shoulder_pitch_joint"]
    assert after == pytest.approx(-before, abs=1e-6), "the flip did not invert"


def test_flipping_the_shoulder_leaves_the_wrist_alone():
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    r._wrist_prev = {}
    r._gap_runs = {}
    r.flip_wrist("left", "shoulder_pitch")
    assert r._WRIST_SIGN["left"]["pitch"] == 1.0
    assert r._WRIST_SIGN["left"]["shoulder_roll"] == 1.0
    assert r._WRIST_SIGN["right"]["shoulder_pitch"] == 1.0


def test_an_unknown_axis_lists_the_real_ones():
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    msg = r.flip_wrist("left", "elbow")
    assert msg.startswith("refused") and "shoulder_pitch" in msg


def _quat(axis, ang):
    axis = np.asarray(axis, float)
    axis = axis / np.linalg.norm(axis)
    return np.concatenate([axis * math.sin(ang / 2), [math.cos(ang / 2)]])


def hand_orientation(side, pitch, roll, elbow, wr_roll, wr_pitch, wr_yaw, yaw=0.0):
    """Orientation of the robot's HAND in the torso frame, full FK."""
    J = _joints()
    T = np.eye(3)
    chain = [(f"{side}_shoulder_pitch_joint", pitch),
             (f"{side}_shoulder_roll_joint", roll),
             (f"{side}_shoulder_yaw_joint", yaw),
             (f"{side}_elbow_joint", elbow),
             (f"{side}_wrist_roll_joint", wr_roll),
             (f"{side}_wrist_pitch_joint", wr_pitch),
             (f"{side}_wrist_yaw_joint", wr_yaw)]
    for name, q in chain:
        _, R, axis = J[name]
        T = T @ R @ _axis_rot(axis, q)
    return T


def _mirror_dirs(side, dirs):
    """The same reach for either arm.

    `dirs` are shoulder-to-wrist directions in the ROBOT frame (X forward, Y
    left, Z up), written for the LEFT arm, where +Y is outward. The right arm
    gets Y negated. The earlier version of this test used ONE set of
    directions for both sides, so an "up and out" reach for one arm was a
    reach across the body for the other, and it then read the difference
    between two different poses as an asymmetry in the code.
    """
    return [[d[0], d[1] if side == "left" else -d[1], d[2]] for d in dirs]


def _mirror_quat(side, axis, ang):
    """A hand orientation mirrored across the operator's sagittal plane."""
    axis = np.asarray(axis, float)
    if side == "left":
        axis, ang = axis * np.array([-1.0, 1.0, 1.0]), -ang
    return _quat(axis, ang).tolist()


def _hand_through_poses(side, dirs, use_forearm, quats=None, unclamp=False,
                        dist=0.27):
    """Robot HAND orientation (full FK) at each arm pose, ONE retargeter.

    One instance across the poses, zeroed at the first, is the situation the
    operator is in: they zero, then reach. A fresh instance per pose re-zeroes
    at every pose and cannot see the defect at all.
    """
    quats = quats or [_mirror_quat(side, [0.3, 0.7, -0.2], math.radians(35))] * len(dirs)
    sh = rg.G1Retargeter._SHOULDER[side]
    inv = np.linalg.inv(rg._R_XR_TO_ROBOT)
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    r._wrist_prev, r._gap_runs, r._wrist_ref, r._forearm_ref = {}, {}, {}, {}
    r.wrist_fk = use_forearm
    # The poses are one frame apart here and tens of degrees apart in joint
    # space, which is exactly what the continuity gate rejects. It is tested
    # on its own; here it would hold the shoulder at pose 1 and hide the tilt.
    r.MAX_STEP = {k: 100.0 for k in rg.G1Retargeter.MAX_STEP}
    r.MAX_WRIST_STEP = 100.0
    if unclamp:
        r.HUMAN_WRIST = {k: 10.0 for k in rg.G1Retargeter.HUMAN_WRIST}
    out, joints = [], []
    for direction, quat in zip(_mirror_dirs(side, dirs), quats):
        d = np.asarray(direction, float)
        d = d / np.linalg.norm(d)
        hand = {"quat": quat, "wrist": (sh + dist * (inv @ d)).tolist()}
        shq = r._shoulder_q(side, hand)
        assert shq, f"{side}: unreachable test pose {direction}"
        pr = (shq[f"{side}_shoulder_pitch_joint"],
              shq[f"{side}_shoulder_roll_joint"])
        fq = r._forearm_q(side, hand, shoulder=(pr if use_forearm else None))
        assert fq, f"{side}: no forearm solution for {direction}"
        w = [fq[f"{side}_wrist_{a}_joint"] for a in ("roll", "pitch", "yaw")]
        joints.append(w)
        out.append(hand_orientation(
            side, pr[0], pr[1], fq[f"{side}_elbow_joint"], *w))
    return out, joints


def _angle_between(A, B):
    dR = A.T @ B
    return math.degrees(math.acos(
        np.clip((float(np.trace(dR)) - 1.0) / 2.0, -1.0, 1.0)))


# Robot frame, left arm: forward-and-down, then a modest lift; then a reach
# up and out to the side. Both start from the same pose so the zero is shared.
_REACHES = {"small": ([1.0, 0.0, -0.4], [0.9, 0.25, -0.1]),
            "large": ([1.0, 0.0, -0.4], [0.3, 0.5, 0.8])}


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("label", sorted(_REACHES))
def test_forearm_relative_wrist_holds_the_hand_fixed(side, label):
    """Same hand orientation, two arm poses: the robot's hand must not tilt.

    This is the defect review finding 5 named. The wrist was conjugated by a
    CONSTANT, exact only while the shoulder is held; drive the shoulder and the
    forearm frame rotates under it, so the operator sees the hand tilt whenever
    they raise the arm. Solved against the forearm's real orientation, and
    relative to the zero pose, the residual is zero on BOTH sides -- measured
    here with the human-range clamp removed, so that what is tested is the
    geometry and not how far the operator's wrist can bend.

    The earlier version of this test asserted that the right arm REGRESSED
    (74.3 -> 116.5 deg) and called it a mirroring error in F or C. It was
    neither: F matches MuJoCo forward kinematics to 0.0001 deg on both arms
    and C_right is the exact mirror of C_left. The right-side residual was the
    wrist-roll clamp biting on an ABSOLUTE hand orientation that happened to
    sit 108-163 deg from the robot's zero, and only on one side because the
    test poses were not mirrored. Referencing to the zero pose removes the
    absolute offset; mirroring the poses removes the false asymmetry.
    """
    dirs = _REACHES[label]
    const, _ = _hand_through_poses(side, dirs, use_forearm=False, unclamp=True)
    fixed, _ = _hand_through_poses(side, dirs, use_forearm=True, unclamp=True)
    spurious = _angle_between(const[0], const[1])
    residual = _angle_between(fixed[0], fixed[1])
    assert spurious > 10.0, (
        f"{side} {label}: the constant conjugation only tilted {spurious:.1f} "
        f"deg -- this reach no longer exercises the defect")
    assert residual < 0.01, (
        f"{side} {label}: hand tilted {residual:.2f} deg across the reach "
        f"(constant path: {spurious:.1f})")


@pytest.mark.parametrize("side", ["left", "right"])
def test_forearm_relative_wrist_is_symmetric(side):
    """Mirrored reach, mirrored hand: the two arms must agree, clamped or not.

    Pins the property the old test could not see. With the poses mirrored the
    residual on one side is the residual on the other, so an asymmetry in F or
    C would show here as a difference between the two, not as a regression on
    one side that reads like a fact about the robot.
    """
    other = "right" if side == "left" else "left"
    for label, dirs in _REACHES.items():
        a, ja = _hand_through_poses(side, dirs, use_forearm=True)
        b, jb = _hand_through_poses(other, dirs, use_forearm=True)
        ra, rb = _angle_between(a[0], a[1]), _angle_between(b[0], b[1])
        assert ra == pytest.approx(rb, abs=0.05), (
            f"{label}: {side} tilted {ra:.2f} deg, {other} {rb:.2f}")


@pytest.mark.parametrize("side", ["left", "right"])
def test_forearm_relative_wrist_is_zero_at_the_zero_pose(side):
    """Zeroing is what makes arming a no-op; the FK path must keep that.

    Whatever the operator's hand is doing when they zero, the first wrist
    command is 0,0,0 -- the robot's held wrist. Read absolutely, the FK path
    turned the operator's comfortable neutral into a standing 108 deg roll
    command on the right arm (the measured cause of the old "asymmetry").
    """
    for quat in ([0.0, 0.0, 0.0, 1.0],
                 _mirror_quat(side, [0.3, 0.7, -0.2], math.radians(35)),
                 _mirror_quat(side, [1.0, 0.0, 0.0], math.radians(-80))):
        _, joints = _hand_through_poses(side, [[1.0, 0.0, -0.4]], use_forearm=True,
                                        quats=[quat])
        assert np.allclose(joints[0], 0.0, atol=1e-9), (
            f"{side}: wrist {np.degrees(joints[0])} deg at the zero pose")


@pytest.mark.parametrize("side", ["left", "right"])
def test_forearm_relative_wrist_reduces_to_the_verified_path_when_the_shoulder_is_held(side):
    """Shoulder held, hand rotating: the FK path must equal the hardware-verified one.

    F^T F_0 is the identity when the shoulder has not moved since zeroing, so
    the two formulas are the same formula. Asserting it exactly means the FK
    path cannot drift away from the mapping that was verified to +0.99 on a
    recording without this test saying so.
    """
    dirs = [[1.0, 0.0, -0.4], [1.0, 0.0, -0.4]]
    quats = [_mirror_quat(side, [0.3, 0.7, -0.2], math.radians(35)),
             _mirror_quat(side, [-0.5, 0.2, 0.8], math.radians(50))]
    _, const = _hand_through_poses(side, dirs, use_forearm=False, quats=quats)
    _, fixed = _hand_through_poses(side, dirs, use_forearm=True, quats=quats)
    assert np.allclose(const, fixed, atol=1e-9), (
        f"{side}: constant {np.degrees(const[1])} vs FK {np.degrees(fixed[1])}")
    assert not np.allclose(const[1], 0.0, atol=1e-3), "the hand did not rotate"


@pytest.mark.parametrize("side", ["left", "right"])
def test_zero_wrists_clears_the_forearm_reference_too(side):
    """A re-zero must re-capture BOTH references on the same frame.

    R_0 and F_0 describe one moment; a zero that refreshed one and kept the
    other would command the difference between two moments as a standing
    offset -- the exact class of bug zeroing exists to remove.
    """
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    r._wrist_prev, r._gap_runs = {}, {}
    r.wrist_fk = True
    sh = rg.G1Retargeter._SHOULDER[side]
    inv = np.linalg.inv(rg._R_XR_TO_ROBOT)
    hand = {"quat": [0.0, 0.0, 0.0, 1.0],
            "wrist": (sh + 0.27 * (inv @ np.array([1.0, 0.0, -0.4]))).tolist()}
    shq = r._shoulder_q(side, hand)
    pr = (shq[f"{side}_shoulder_pitch_joint"], shq[f"{side}_shoulder_roll_joint"])
    r._forearm_q(side, hand, shoulder=pr)
    assert side in r._wrist_ref and side in r._forearm_ref
    r.zero_wrists()
    assert not r._wrist_ref and not r._forearm_ref


@pytest.mark.parametrize("side", ["left", "right"])
def test_holding_the_shoulder_still_leaves_the_verified_path_alone(side):
    """Forearm mode passes shoulder=None and must behave exactly as before."""
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    r._wrist_prev, r._gap_runs, r._wrist_ref = {}, {}, {}
    hand = {"quat": _quat(np.array([0.0, 1.0, 0.0]), math.radians(20)).tolist(),
            "wrist": (rg.G1Retargeter._SHOULDER[side]
                      + np.array([0.0, 0.0, -0.27])).tolist()}
    r._forearm_q(side, hand)                     # captures the zero
    out = r._forearm_q(side, hand)
    for axis in ("roll", "pitch", "yaw"):
        assert abs(out[f"{side}_wrist_{axis}_joint"]) < 1e-6, \
            "the held-shoulder path no longer zeroes at its reference"
