"""Shoulder yaw joins the solve only when roll alone cannot reach.

First full-stack episode (20260915-001254-first-full-stack): the operator's
right hand worked across the body for 78 % of a 116 s armed run. The shoulder
solve turned that into inward roll targets up to +77 deg. This robot's right
shoulder roll is calibrated to +5 deg inward -- the arm meets the torso -- so
58 % of the right shoulder roll commands were unreachable, the arm sat on the
limit and the leash dragged every right joint behind. The left hand stayed
beside its shoulder and tracked to 1-2 deg. Same code, same session: one arm
following and one not.

A pitch/roll-only solve has no way to reach across the body under that limit.
Shoulder yaw is the joint that can: yawing the shoulder inward swings the whole
arm toward the midline without rolling the upper arm into the torso.

Yaw was left out on the principle that WebXR reports no elbow and so gives no
evidence for it. That still holds for the null-space freedom, and the solve
keeps yaw at exactly zero whenever roll suffices. It moves only when the
alternative is a command the robot cannot follow, which the episode shows is
the worse outcome.
"""
import json
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))
import retarget_g1 as rg  # noqa: E402

HERE = os.path.dirname(__file__)
FIXTURE = os.path.join(HERE, "fixtures", "first_full_stack_hands.json")
REAL_CAL = os.path.join(HERE, "..", "onboard", "calibration", "unitree-g1-nx-2.json")


def _retargeter(side_elbow=None, yaw=True):
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    r._wrist_prev, r._gap_runs, r._wrist_ref, r._forearm_ref = {}, {}, {}, {}
    # The continuity gate is tested on its own; here consecutive test poses
    # are far apart on purpose.
    r.MAX_STEP = {k: 100.0 for k in rg.G1Retargeter.MAX_STEP}
    r.MAX_WRIST_STEP = 100.0
    r.shoulder_yaw = yaw
    if side_elbow is not None:
        r._elbow_q = lambda side, hand: side_elbow
    return r


def _hand_at(side, direction_robot, dist=0.27):
    """A hand `dist` metres from the assumed shoulder along a ROBOT-frame direction."""
    sh = rg.G1Retargeter._SHOULDER[side]
    inv = np.linalg.inv(rg._R_XR_TO_ROBOT)
    d = np.asarray(direction_robot, float)
    d = d / np.linalg.norm(d)
    return {"wrist": (sh + dist * (inv @ d)).tolist()}


def _fk_direction(side, pitch, roll, yaw, elbow):
    """Ry(p) Rx(r) w_hat(yaw): where the hand ends up, in the solve's own frame."""
    w = rg.shoulder_to_wrist(side, elbow, yaw)
    return rg._ry(pitch) @ rg._rx(roll) @ (w / np.linalg.norm(w))


def _target_direction(side, hand):
    v = np.asarray(hand["wrist"]) - rg.G1Retargeter._SHOULDER[side]
    return rg.G1Retargeter._SHOULDER_PRE[side].T @ (rg._R_XR_TO_ROBOT @ (v / np.linalg.norm(v)))


def _angle(a, b):
    return math.degrees(math.acos(np.clip(float(np.dot(a, b)), -1.0, 1.0)))


# ---- the episode ------------------------------------------------------------

def _right_hand_frames():
    blob = json.load(open(FIXTURE))
    return [f["hands"]["right"] for f in blob["frames"]
            if "right" in f["hands"] and f["hands"]["right"]["elbow"] is not None]


def _unreachable_fraction(yaw_enabled, shoulder=None):
    """Fraction of the recorded right-hand frames whose shoulder solution had
    to be clamped to the robot's limits (i.e. the hand could not be placed)."""
    limits, src = rg.load_robot_limits(REAL_CAL)
    assert src is not None, "the robot's calibration file must load for this test"
    r = _retargeter(yaw=yaw_enabled)
    r.ROBOT_LIMITS = limits
    if shoulder is not None:
        r._SHOULDER = shoulder
    clamped, n = 0, 0
    for h in _right_hand_frames():
        r._elbow_q = lambda side, hand, e=h["elbow"]: e
        if not r._shoulder_q("right", {"wrist": h["wrist"]}):
            continue
        n += 1
        clamped += r._shoulder_miss["right"] > 1e-9
    return clamped / n, n


_OLD_SHOULDER_GUESS = {"left": np.array([-0.18, -0.20, 0.0]),
                       "right": np.array([+0.18, -0.20, 0.0])}


def test_the_recorded_right_hand_is_now_reachable():
    """The episode, replayed against the robot's calibrated limits.

    58 % of the right shoulder solutions were unreachable as deployed; with
    yaw allowed to take the inward part and the shoulder assumed where the
    URDF puts it, under 5 %.
    """
    frac, n = _unreachable_fraction(yaw_enabled=True)
    assert n > 1000
    assert frac < 0.05, f"{100*frac:.0f}% of {n} right-hand frames still unreachable"


def test_the_defect_is_reproduced_as_deployed():
    """Pins the failure the episode showed -- yaw off, hand-typed shoulder --
    so the fix is measured against something."""
    frac, _ = _unreachable_fraction(yaw_enabled=False, shoulder=_OLD_SHOULDER_GUESS)
    assert frac > 0.4, f"only {100*frac:.0f}% unreachable as deployed -- the fixture no longer shows the defect"


def test_each_half_of_the_fix_helps_on_its_own():
    """Yaw alone and the derived shoulder alone each cut the unreachable
    fraction; together they get under 5 %. Recorded so the two changes can be
    judged separately if one has to be reverted."""
    base, _ = _unreachable_fraction(False, _OLD_SHOULDER_GUESS)
    yaw_only, _ = _unreachable_fraction(True, _OLD_SHOULDER_GUESS)
    shoulder_only, _ = _unreachable_fraction(False)
    assert yaw_only < base * 0.5, f"yaw alone: {100*base:.0f}% -> {100*yaw_only:.0f}%"
    assert shoulder_only < base, f"derived shoulder alone: {100*base:.0f}% -> {100*shoulder_only:.0f}%"


def test_the_assumed_shoulder_is_measured_and_mirrored():
    """The URDF derivation was an improvement on a typed guess; a measurement
    beats both.

    The operator matched their own hand and the ROBOT's hand at three poses
    with the arm limp. The derived shoulder made a hanging arm 0.295 m from
    the shoulder and an extended arm 0.427 m -- both straight arms, which
    cannot differ, since an arm does not change length when pointed
    elsewhere. Solved against the robot's FK the residual falls from 8.6 cm
    to 2.7 cm, and the elbow error from 21-77 deg to 5.3 deg RMS.

    Mirrored, not measured twice: one pair of shoulders, and the left/right
    symmetry of the solve is load-bearing for the other tests here.
    """
    sh = rg.G1Retargeter._SHOULDER
    assert np.allclose(sh["left"], sh["right"] * np.array([-1.0, 1.0, 1.0])), \
        "the two shoulders must stay mirrored; several frame tests rely on it"
    assert 0.10 < sh["right"][0] < 0.16, "right shoulder 10-16 cm to the operator's right"
    assert sh["right"][2] > 0.0, "shoulders are BEHIND the eyes (+z in WebXR)"
    assert -0.30 < sh["right"][1] < -0.10, "and below them"


# ---- the solve --------------------------------------------------------------

@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("direction", [[1.0, 0.0, -0.4], [1.0, 0.0, 0.3], [0.6, 0.0, -0.8],
                                       [0.7, 0.5, 0.0], [0.5, 0.8, -0.2]])
def test_yaw_is_exactly_zero_whenever_roll_suffices(side, direction):
    """Reaching forward, down, or OUTWARD needs no yaw, and gets none.

    Zero, not small: with the shoulder held this must be the two-joint solve
    verified against the URDF, bit for bit, plus a yaw of 0.0.
    """
    d = [direction[0], direction[1] if side == "left" else -direction[1], direction[2]]
    r = _retargeter(side_elbow=0.3)
    out = r._shoulder_q(side, _hand_at(side, d))
    assert out[f"{side}_shoulder_yaw_joint"] == 0.0
    two = _retargeter(side_elbow=0.3, yaw=False)._shoulder_q(side, _hand_at(side, d))
    assert out[f"{side}_shoulder_pitch_joint"] == two[f"{side}_shoulder_pitch_joint"]
    assert out[f"{side}_shoulder_roll_joint"] == two[f"{side}_shoulder_roll_joint"]
    assert f"{side}_shoulder_yaw_joint" not in two, "yaw off must keep the old wire shape"


@pytest.mark.parametrize("side", ["left", "right"])
def test_a_cross_body_reach_uses_yaw_and_keeps_roll_inside_the_limit(side):
    limits, _ = rg.load_robot_limits(REAL_CAL)
    r = _retargeter(side_elbow=0.3)
    r.ROBOT_LIMITS = limits
    inward = -1.0 if side == "left" else +1.0           # robot Y toward the midline
    out = r._shoulder_q(side, _hand_at(side, [0.5, 0.9 * inward, -0.1]))
    lo, hi = limits[f"{side}_shoulder_roll_joint"]
    roll, yaw = out[f"{side}_shoulder_roll_joint"], out[f"{side}_shoulder_yaw_joint"]
    assert lo - 1e-6 <= roll <= hi + 1e-6, f"roll {math.degrees(roll):.1f} outside the calibrated limit"
    assert abs(yaw) > math.radians(10), f"yaw only {math.degrees(yaw):.1f} deg on a hand well across the body"


@pytest.mark.parametrize("side", ["left", "right"])
def test_the_yawed_solution_is_exact_by_forward_kinematics(side):
    """Yaw is not a fudge: the hand still lands on the requested direction.

    Checked by running (pitch, roll, yaw) through the URDF chain, the same
    check the two-joint solve passed at 0.0000 deg.
    """
    limits, _ = rg.load_robot_limits(REAL_CAL)
    r = _retargeter(side_elbow=0.3)
    r.ROBOT_LIMITS = limits
    inward = -1.0 if side == "left" else +1.0
    # Cross-body reaches that ARE reachable once yaw joins (20-35 deg of it).
    # A hand far enough across is unreachable at any yaw with a lightly bent
    # elbow; that case is clamped and reported, and tested elsewhere.
    for direction in ([0.8, 0.5 * inward, 0.2], [0.7, 0.4 * inward, -0.2], [0.9, 0.3 * inward, 0.0]):
        hand = _hand_at(side, direction)
        out = r._shoulder_q(side, hand)
        assert r._shoulder_miss[side] <= 1e-9, f"{side} {direction}: expected reachable"
        p, ro, y = (out[f"{side}_shoulder_{j}_joint"] for j in ("pitch", "roll", "yaw"))
        assert abs(y) > math.radians(1.0), f"{side} {direction}: yaw did not engage"
        err = _angle(_fk_direction(side, p, ro, y, 0.3), _target_direction(side, hand))
        assert err < 0.01, f"{side} {direction}: hand lands {err:.3f} deg off with yaw {math.degrees(y):.1f}"


@pytest.mark.parametrize("side", ["left", "right"])
def test_yaw_varies_continuously_as_the_hand_crosses_the_body(side):
    """Sweep the hand from outward to across the body: no jumps, no sign flips.

    A 5 deg search grid would step the yaw in 5 deg jumps as the reachability
    boundary moved; the search refines to the boundary so the command moves
    with the hand.
    """
    limits, _ = rg.load_robot_limits(REAL_CAL)
    r = _retargeter(side_elbow=0.3)
    r.ROBOT_LIMITS = limits
    inward = -1.0 if side == "left" else +1.0
    yaws, rolls = [], []
    for y in np.linspace(-0.6, 1.0, 120):
        out = r._shoulder_q(side, _hand_at(side, [0.7, y * inward, -0.2]))
        yaws.append(out[f"{side}_shoulder_yaw_joint"])
        rolls.append(out[f"{side}_shoulder_roll_joint"])
    yaws, rolls = np.degrees(yaws), np.degrees(rolls)
    assert yaws[0] == 0.0 and abs(yaws[-1]) > 10.0
    assert np.abs(np.diff(yaws)).max() < 3.0, f"yaw jumped {np.abs(np.diff(yaws)).max():.1f} deg between neighbouring hand positions"
    assert np.abs(np.diff(rolls)).max() < 3.0
    nz = yaws[yaws != 0.0]
    assert np.all(np.sign(nz) == np.sign(nz[0])), "yaw changed sign mid-sweep"


def test_the_continuity_gate_knows_shoulder_yaw():
    """A yaw jump of 40 deg in one frame must be gated like pitch and roll, not
    let through at the wrist's threshold."""
    assert any("shoulder_yaw" in k for k in rg.G1Retargeter.MAX_STEP)
    assert rg.G1Retargeter.MAX_STEP["shoulder_yaw"] <= rg.G1Retargeter.MAX_STEP["shoulder_pitch"]


def test_yaw_has_a_sign_and_a_limit_like_the_other_shoulder_axes():
    for side in ("left", "right"):
        assert rg.G1Retargeter._WRIST_SIGN[side]["shoulder_yaw"] == 1.0
        assert f"{side}_shoulder_yaw_joint" in rg.ARM_JOINT_LIMITS


# ---- the robot's limits -----------------------------------------------------

def test_robot_limits_come_from_the_calibration_file_when_present():
    limits, src = rg.load_robot_limits(REAL_CAL)
    assert src == REAL_CAL
    lo, hi = limits["right_shoulder_roll_joint"]
    assert hi == pytest.approx(math.radians(5.0), abs=math.radians(1.0)), \
        "the calibrated right inward roll limit is about +5 deg; this test rests on it"
    # Never wider than the datasheet, whatever the file says.
    for n, (dlo, dhi) in rg.ARM_JOINT_LIMITS.items():
        assert limits[n][0] >= dlo - 1e-9 and limits[n][1] <= dhi + 1e-9, n


def test_robot_limits_fall_back_to_the_datasheet(tmp_path):
    limits, src = rg.load_robot_limits(str(tmp_path / "missing.json"))
    assert src is None and limits == rg.ARM_JOINT_LIMITS
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"limits": [[0, 1]] * 3}))
    limits, src = rg.load_robot_limits(str(bad))
    assert src is None and limits == rg.ARM_JOINT_LIMITS


def test_the_bridge_ships_the_calibration_file():
    """The bridge image cannot see onboard/, so it carries its own copy of the
    measured limits, or the container solves against the datasheet and asks
    for +77 deg of inward roll again."""
    teleop = os.path.join(HERE, "..", "teleop")
    dockerfile = open(os.path.join(teleop, "Dockerfile")).read()
    assert "COPY calibration ./calibration" in dockerfile
    assert "TELEOP_ARM_CAL=/app/calibration/arm-limits.json" in dockerfile
    assert open(os.path.join(teleop, "calibration", "arm-limits.json"), "rb").read() \
        == open(REAL_CAL, "rb").read()


# ---- the wrist follows the yawed forearm -----------------------------------

@pytest.mark.parametrize("side", ["left", "right"])
def test_the_wrist_is_solved_against_the_yawed_forearm(side):
    """Same hand orientation, two arm poses of which the second needs yaw:
    the robot's hand must not tilt (unclamped), exactly as it does not when
    only pitch and roll move."""
    from tests.test_shoulder import hand_orientation, _mirror_quat
    limits, _ = rg.load_robot_limits(REAL_CAL)
    r = _retargeter(side_elbow=0.3)
    r.ROBOT_LIMITS = limits
    r.wrist_fk = True
    r.HUMAN_WRIST = {k: 10.0 for k in rg.G1Retargeter.HUMAN_WRIST}
    quat = _mirror_quat(side, [0.3, 0.7, -0.2], math.radians(35))
    inward = -1.0 if side == "left" else +1.0
    hands_out = []
    for direction in ([1.0, 0.0, -0.4], [0.5, 0.9 * inward, -0.1]):
        hand = dict(_hand_at(side, direction), quat=quat)
        sh = r._shoulder_q(side, hand)
        pr = tuple(sh[f"{side}_shoulder_{j}_joint"] for j in ("pitch", "roll", "yaw"))
        fq = r._forearm_q(side, hand, shoulder=pr)
        w = [fq[f"{side}_wrist_{a}_joint"] for a in ("roll", "pitch", "yaw")]
        hands_out.append(hand_orientation(side, pr[0], pr[1], 0.3, *w, yaw=pr[2]))
    assert abs(pr[2]) > math.radians(10), "the second pose did not engage yaw"
    dR = hands_out[0].T @ hands_out[1]
    tilt = math.degrees(math.acos(np.clip((np.trace(dR) - 1) / 2, -1, 1)))
    assert tilt < 0.01, f"{side}: hand tilted {tilt:.2f} deg when the shoulder yawed"


# ---- the wire ---------------------------------------------------------------

def _sink():
    import threading
    import sink_realg1 as sk
    s = sk.RealG1Sink.__new__(sk.RealG1Sink)
    s._latest, s._latest_t = None, 0.0
    s._lock = threading.Lock()
    s.n_sent = s.n_retargeted = 0
    return s


def _targets(names):
    out = {n: 0.1 for n in names}
    for n in rg.DEX3_LEFT + rg.DEX3_RIGHT:
        out[n] = 0.1
    return out


def test_a_seven_joint_side_with_shoulder_yaw_is_accepted():
    import asyncio
    import sink_realg1 as sk
    seven = sorted(sk.RealG1Sink._FOREARM_SET["right"] | sk.RealG1Sink._SHOULDER_SET["right"])
    assert "right_shoulder_yaw_joint" in seven and len(seven) == 7
    s = _sink()
    asyncio.run(s.apply(_targets(seven)))
    assert s._latest is not None and set(s._latest["arm_partial"]) == set(seven)


def test_six_without_yaw_is_still_accepted_for_an_older_bridge():
    import asyncio
    import sink_realg1 as sk
    six = sorted(sk.RealG1Sink._FOREARM_SET["right"] | sk.RealG1Sink._SHOULDER_PR_SET["right"])
    assert len(six) == 6
    s = _sink()
    asyncio.run(s.apply(_targets(six)))
    assert s._latest is not None


def test_a_complete_arm_frame_from_the_retargeter_still_gets_the_arming_offset():
    """Both shoulders with yaw name all 14 arm joints, which is the sink's
    full-arm path. That path used to skip the arming offset, so the moment
    yaw shipped the arming lurch (53.8 deg on an elbow, measured) came back."""
    import asyncio
    import sink_realg1 as sk
    s = _sink()
    s.onboard_state, s.onboard_msg = "ARMED", ""
    # Use a held pose inside this robot's calibrated envelope. The right
    # shoulder-roll upper bound is only 0.093 rad; the old synthetic 0.3 pose
    # was physically infeasible and is now intentionally projected.
    held = [min(hi, max(lo, 0.3)) for lo, hi in
            (rg.ROBOT_LIMITS[name] for name in sk._ARM)]
    s.onboard_q = held
    s._arm_offset, s._warned_no_q = None, False
    asyncio.run(s.apply(_targets(list(sk._ARM))))         # every target 0.1
    assert "arm" in s._latest
    assert s._latest["arm"] == pytest.approx(held), \
        "the first armed command must equal the pose the arm is holding"


def test_seven_minus_a_wrist_is_refused():
    """Whole set or nothing still holds at seven."""
    import asyncio
    import sink_realg1 as sk
    seven = sorted(sk.RealG1Sink._FOREARM_SET["right"] | sk.RealG1Sink._SHOULDER_SET["right"])
    s = _sink()
    asyncio.run(s.apply(_targets([n for n in seven if n != "right_wrist_roll_joint"])))
    assert s._latest is None
