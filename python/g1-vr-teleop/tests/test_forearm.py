"""Forearm mode: elbow + wrist follow the operator, the shoulder is held.

Two properties matter and they pull against each other:

  - a DELIBERATELY partial frame (forearm mode) must be accepted, with the
    shoulder held at whatever pose control was taken at;
  - an ACCIDENTALLY partial frame -- one that lost a joint in transit -- must
    still be refused, exactly as before. Accepting partial sets in general
    would turn a corrupted frame into a plausible-looking command.

The distinguishing rule is that the partial set must be EXACTLY the forearm
joints, nothing else.
"""
import os
import sys
import threading

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "onboard"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))
import g1_onboard as ob  # noqa: E402

# A hold pose inside the CALIBRATED limits. Using arbitrary values here fails
# in a way that looks like the merge disturbing joints, when it is really
# sanitize_arm clamping -- e.g. right_shoulder_roll is calibrated to
# -129 deg .. +1 deg, so 0.99 rad comes back as 0.015.
HOLD = [round(-0.05 - 0.01 * i, 3) for i in range(14)]


def _cmd():
    c = ob.Commander.__new__(ob.Commander)
    c.lock = threading.RLock()
    c._hold_pose = list(HOLD)
    c.measured_arm_q = list(HOLD)
    return c


def test_named_joints_are_laid_over_the_held_pose():
    c = _cmd()
    out = c.merge_partial_arm({"left_elbow_joint": 0.5,
                               "right_wrist_pitch_joint": -0.4})
    i_el = ob.ARM_JOINT_INDEX["left_elbow_joint"]
    i_wp = ob.ARM_JOINT_INDEX["right_wrist_pitch_joint"]
    assert out[i_el] == pytest.approx(0.5)
    assert out[i_wp] == pytest.approx(-0.4)
    for i, v in enumerate(HOLD):
        if i not in (i_el, i_wp):
            assert out[i] == pytest.approx(v), f"joint {i} was disturbed"


def test_the_shoulder_is_never_touched_by_a_forearm_frame():
    c = _cmd()
    out = c.merge_partial_arm({f"{s}_{j}_joint": 0.3
                               for s in ("left", "right")
                               for j in ("elbow", "wrist_roll", "wrist_pitch", "wrist_yaw")})
    for name, i in ob.ARM_JOINT_INDEX.items():
        if "shoulder" in name:
            assert out[i] == pytest.approx(HOLD[i]), f"{name} moved"


def test_an_empty_partial_commands_nothing_new():
    c = _cmd()
    assert c.merge_partial_arm({}) == pytest.approx(ob.sanitize_arm(HOLD))


def test_an_unknown_joint_name_is_refused():
    c = _cmd()
    with pytest.raises(ValueError, match="unknown arm joint"):
        c.merge_partial_arm({"left_pinky_joint": 0.1})


def test_a_non_finite_value_is_refused():
    c = _cmd()
    with pytest.raises(ValueError):
        c.merge_partial_arm({"left_elbow_joint": float("nan")})


def test_limits_still_apply_through_the_merge():
    c = _cmd()
    out = c.merge_partial_arm({"left_elbow_joint": 99.0})
    lo, hi = ob.effective_arm_limits()[ob.ARM_JOINT_INDEX["left_elbow_joint"]]
    assert lo - 1e-9 <= out[ob.ARM_JOINT_INDEX["left_elbow_joint"]] <= hi + 1e-9


def test_it_refuses_before_control_is_taken():
    c = _cmd()
    c._hold_pose = None
    c.measured_arm_q = None
    with pytest.raises(ValueError, match="no held pose"):
        c.merge_partial_arm({"left_elbow_joint": 0.2})


# --------------------------------------------------------------------------- #
# the sink's accept/refuse rule
# --------------------------------------------------------------------------- #
def _sink():
    import sink_realg1
    s = sink_realg1.RealG1Sink.__new__(sink_realg1.RealG1Sink)
    s._latest, s._latest_t = None, 0.0
    s._lock = threading.Lock()
    s.n_sent = s.n_retargeted = 0
    return s


def _targets(arm_names):
    import sink_realg1 as sk
    t = {n: 0.1 for n in sk.DEX3_LEFT + sk.DEX3_RIGHT}
    t.update({n: 0.2 for n in arm_names})
    return t


def test_the_forearm_set_is_accepted_as_partial():
    import asyncio
    import sink_realg1 as sk
    s = _sink()
    # One side's full driven set. Both sides together now name all 14 arm
    # joints, which is a complete arm frame rather than a partial one.
    asyncio.run(s.apply(_targets(sorted(sk.RealG1Sink._FOREARM_SET["left"]
                                        | sk.RealG1Sink._SHOULDER_SET["left"]))))
    assert s._latest is not None, "a legitimate forearm frame was dropped"
    assert "arm_partial" in s._latest and "arm" not in s._latest


def test_a_frame_that_lost_one_shoulder_joint_is_still_refused():
    """The regression this rule exists for."""
    import asyncio
    import sink_realg1 as sk
    s = _sink()
    asyncio.run(s.apply(_targets(sk._ARM[1:])))      # 13 of 14, one missing
    assert s._latest is None, "an incomplete arm frame was accepted as partial"


def test_a_full_arm_frame_still_goes_as_a_vector():
    import asyncio
    import sink_realg1 as sk
    s = _sink()
    asyncio.run(s.apply(_targets(sk._ARM)))
    assert s._latest is not None and "arm" in s._latest
    assert "arm_partial" not in s._latest


def test_a_broken_wrist_triple_is_refused():
    """Two of a side's three wrist joints is a frame that lost one.

    Holding the lost joint while its two neighbours move is a move nobody
    asked for -- the same class of bug as defaulting a missing joint to zero,
    just quieter.
    """
    import asyncio
    import sink_realg1 as sk
    s = _sink()
    two_of_three = ["left_wrist_roll_joint", "left_wrist_pitch_joint"]
    asyncio.run(s.apply(_targets(two_of_three)))
    assert s._latest is None, "a broken wrist triple was accepted"


def test_one_hand_alone_is_a_legitimate_partial():
    import asyncio
    import sink_realg1 as sk
    s = _sink()
    asyncio.run(s.apply(_targets(sorted(sk.RealG1Sink._FOREARM_SET["right"]))))
    assert s._latest is not None, "one-handed tracking was dropped"
    assert set(s._latest["arm_partial"]) == sk.RealG1Sink._FOREARM_SET["right"]


def test_a_partial_set_is_elbow_plus_three_wrist_joints():
    import sink_realg1 as sk
    for side in ("left", "right"):
        assert sk.RealG1Sink._FOREARM_SET[side] == {
            f"{side}_elbow_joint", f"{side}_wrist_roll_joint",
            f"{side}_wrist_pitch_joint", f"{side}_wrist_yaw_joint"}
    # All three shoulder axes may travel now: the solve yaws the shoulder when
    # roll alone would run into the torso limit (see test_shoulder_yaw.py).
    for side in ("left", "right"):
        assert sk.RealG1Sink._SHOULDER_SET[side] <= sk.RealG1Sink.FOREARM_JOINTS
        assert f"{side}_shoulder_yaw_joint" in sk.RealG1Sink._SHOULDER_SET[side]


# --------------------------------------------------------------------------- #
# elbow from reach
# --------------------------------------------------------------------------- #
def _wrist_at(r, side, dist):
    """A wrist position `dist` metres from the modelled shoulder."""
    import retarget_g1
    sh = retarget_g1.G1Retargeter._SHOULDER[side]
    return (sh + np.array([0.0, 0.0, -float(dist)])).tolist()


def test_reaching_out_straightens_the_elbow():
    r = _retargeter()
    near, far = type(r)._ELBOW_CAL["left"]
    bent = r._elbow_q("left", {"wrist": _wrist_at(r, "left", near)})
    straight = r._elbow_q("left", {"wrist": _wrist_at(r, "left", far)})
    assert straight > bent, "reaching out must not bend the elbow further"
    # The robot's elbow is straight near +73 deg, not at zero -- see _ELBOW_SPAN.
    assert straight == pytest.approx(type(r)._ELBOW_SPAN[1], abs=1e-6)
    assert bent == pytest.approx(type(r)._ELBOW_SPAN[0], abs=1e-6)


def test_the_elbow_stays_inside_its_joint_limits():
    import retarget_g1
    r = _retargeter()
    lo, hi = retarget_g1.ARM_JOINT_LIMITS["left_elbow_joint"]
    for d in (0.0, 0.05, 0.2, 0.35, 0.45, 0.9, 3.0):
        q = r._elbow_q("left", {"wrist": _wrist_at(r, "left", d)})
        assert lo - 1e-9 <= q <= hi + 1e-9, f"d={d} gave {q}"


def test_reach_beyond_the_calibrated_ends_saturates_rather_than_extrapolates():
    r = _retargeter()
    near, far = type(r)._ELBOW_CAL["right"]
    assert (r._elbow_q("right", {"wrist": _wrist_at(r, "right", far + 0.5)})
            == pytest.approx(r._elbow_q("right", {"wrist": _wrist_at(r, "right", far)})))
    assert (r._elbow_q("right", {"wrist": _wrist_at(r, "right", 0.0)})
            == pytest.approx(r._elbow_q("right", {"wrist": _wrist_at(r, "right", near)})))


def test_a_frame_with_no_wrist_position_drives_no_elbow():
    """Absent, not zero. Zero is a specific elbow pose."""
    r = _retargeter()
    assert r._elbow_q("left", {}) is None
    out = r._forearm_q("left", {"quat": [0.0, 0.2588, 0.0, 0.9659]})
    assert "left_elbow_joint" not in out


def test_calibration_captures_the_operators_own_reach():
    r = _retargeter()
    r.elbow_cal("left", "near", _wrist_at(r, "left", 0.25))
    r.elbow_cal("left", "far", _wrist_at(r, "left", 0.55))
    assert r._ELBOW_CAL["left"][0] == pytest.approx(0.25, abs=1e-6)
    assert r._ELBOW_CAL["left"][1] == pytest.approx(0.55, abs=1e-6)
    assert r._ELBOW_CAL["right"] == type(r)._ELBOW_CAL["right"], "other hand moved"


def test_a_degenerate_calibration_never_reaches_the_mapping():
    """near == far would turn a millimetre of noise into the full joint range.

    Two defences, and this pins both: elbow_cal refuses to STORE such a span,
    and _elbow_q refuses to USE one if it arrives another way (a hand-edited
    calibration file, say).
    """
    r = _retargeter()
    r.elbow_cal("left", "near", _wrist_at(r, "left", 0.30))
    assert r.elbow_cal("left", "far", _wrist_at(r, "left", 0.31)).startswith("refused")
    r._ELBOW_CAL = dict(r._ELBOW_CAL, left=(0.30, 0.31))      # forced in
    assert r._elbow_q("left", {"wrist": _wrist_at(r, "left", 0.305)}) is None


def test_calibrating_a_bad_side_is_refused():
    r = _retargeter()
    assert r.elbow_cal("middle", "near", [0, 0, 0]).startswith("refused")
    assert r.elbow_cal("left", "sideways", [0, 0, 0]).startswith("refused")


# --------------------------------------------------------------------------- #
# wrist mapping
# --------------------------------------------------------------------------- #
# The guard against writing the real wrist_sign.json is suite-wide now:
# tests/conftest.py::_sign_file_in_tmp.


def _retargeter():
    # No importorskip: the wrist mapping is pure numpy, and the heavy
    # dex_retargeting/pinocchio imports live in __init__ -- which __new__
    # skips. Skipping here would have quietly retired these tests everywhere
    # except the robot image.
    import retarget_g1
    r = retarget_g1.G1Retargeter.__new__(retarget_g1.G1Retargeter)
    return r


def _forearm(r, side="left", quat=(0.0, 0.2588, 0.0, 0.9659)):   # 30 deg about Y
    """Query at `quat`, having zeroed at identity.

    The zeroing matters: without it the FIRST call captures its own pose as the
    reference and every angle comes back 0, so a test comparing before/after a
    sign flip would compare 0 with -0 and pass no matter what the code did.
    """
    r._wrist_zero(side, np.eye(3))
    out = r._forearm_q(side, {"quat": list(quat)})
    assert any(abs(v) > 1e-3 for v in out.values()), \
        "the probe pose produced no wrist motion -- this test would be vacuous"
    return out


def test_the_reference_pose_itself_commands_no_motion():
    """Zero, then hold still: the robot must not move."""
    r = _retargeter()
    q = [0.0, 0.2588, 0.0, 0.9659]
    r._forearm_q("left", {"quat": q})                 # captures the reference
    out = r._forearm_q("left", {"quat": q})           # same pose again
    assert all(abs(v) < 1e-6 for v in out.values()), out


def test_the_operators_neutral_pose_is_not_a_wrist_command():
    """What zeroing is actually for.

    The operator's comfortable neutral hand orientation has no reason to
    coincide with the robot's held wrist angles, and without a reference that
    difference is a standing command: arm the robot and the wrists immediately
    drive to some arbitrary pose and stay there.

    Body rotation is NOT what this handles -- web/teleop.js already reports the
    wrist in the head's heading frame, so a turn never reaches here. That
    invariance is pinned in test_teleop_js.py against the real JS.
    """
    r = _retargeter()
    neutral = _quat(np.array([0.3, 0.8, -0.2]), np.radians(37))  # nothing special
    r._forearm_q("left", {"quat": neutral.tolist()})
    out = r._forearm_q("left", {"quat": neutral.tolist()})
    assert all(abs(v) < 1e-6 for v in out.values()), \
        f"the operator's neutral pose is commanding the wrists: {out}"


def _quat(axis, ang):
    axis = axis / np.linalg.norm(axis)
    return np.concatenate([axis * np.sin(ang / 2), [np.cos(ang / 2)]])


def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return np.array([aw * bx + ax * bw + ay * bz - az * by,
                     aw * by - ax * bz + ay * bw + az * bx,
                     aw * bz + ax * by - ay * bx + az * bw,
                     aw * bw - ax * bx - ay * by - az * bz])


def test_zeroing_again_re_references():
    r = _retargeter()
    a = _quat(np.array([0.0, 1.0, 0.0]), np.radians(20))
    b = _quat(np.array([0.0, 1.0, 0.0]), np.radians(40))
    r._forearm_q("left", {"quat": a.tolist()})
    moved = r._forearm_q("left", {"quat": b.tolist()})
    assert any(abs(v) > 1e-3 for v in moved.values()), "the probe did not move"
    r.zero_wrists()
    r._forearm_q("left", {"quat": b.tolist()})        # new reference is b
    assert all(abs(v) < 1e-6 for v in r._forearm_q("left", {"quat": b.tolist()}).values())


def test_a_zero_is_captured_per_hand():
    r = _retargeter()
    q = _quat(np.array([1.0, 0.0, 0.0]), np.radians(30))
    r._forearm_q("left", {"quat": q.tolist()})
    assert set(r._wrist_ref) == {"left"}, "zeroing one hand referenced the other"
    r._forearm_q("right", {"quat": q.tolist()})
    assert set(r._wrist_ref) == {"left", "right"}


def test_the_derived_rotations_are_proper_and_mirrored():
    """Both hands came out of the derivation as exact mirrors; keep them so."""
    import retarget_g1
    W = retarget_g1.G1Retargeter._WRIST_ROT
    for side, M in W.items():
        assert np.allclose(M @ M.T, np.eye(3), atol=1e-9), f"{side} is not a rotation"
        assert np.linalg.det(M) > 0, f"{side} is a reflection, not a rotation"
    # The mirror is not the same on both ends, and that is the interesting
    # part. The robot's two hands mirror through its sagittal plane -- negate
    # Y in the robot frame. The operator's two hands mirror through the palm --
    # negate X in the XR wrist frame. So the relation between the two derived
    # matrices is a mirror on each side, which composes back to a rotation.
    robot_mirror = np.diag([1.0, -1.0, 1.0])
    xr_mirror = np.diag([-1.0, 1.0, 1.0])
    assert np.allclose(W["right"], robot_mirror @ W["left"] @ xr_mirror, atol=2e-3), \
        "the hands are no longer mirror images of each other"


def test_the_wrist_emits_three_joints_and_no_elbow():
    r = _retargeter()
    out = _forearm(r)
    assert set(out) == {f"left_wrist_{j}_joint" for j in ("roll", "pitch", "yaw")}


def test_flipping_an_axis_negates_that_axis_only():
    r = _retargeter()
    before = _forearm(r)
    assert "now" in r.flip_wrist("left", "pitch")
    after = _forearm(r)
    assert after["left_wrist_pitch_joint"] == pytest.approx(
        -before["left_wrist_pitch_joint"], abs=1e-9)
    for j in ("roll", "yaw"):
        n = f"left_wrist_{j}_joint"
        assert after[n] == pytest.approx(before[n]), f"{n} moved too"


def test_flipping_one_hand_leaves_the_other_alone():
    r = _retargeter()
    before = _forearm(r, "right")
    r.flip_wrist("left", "yaw")
    assert _forearm(r, "right") == pytest.approx(before)


def test_a_flip_does_not_edit_the_class_default():
    """Otherwise one flip would silently re-aim every later instance."""
    import retarget_g1
    r = _retargeter()
    r.flip_wrist("left", "roll")
    assert retarget_g1.G1Retargeter._WRIST_SIGN["left"]["roll"] == 1.0


def test_a_bad_axis_is_refused_rather_than_ignored():
    r = _retargeter()
    assert r.flip_wrist("left", "twist").startswith("refused")
    assert r.flip_wrist("middle", "yaw").startswith("refused")


def test_flips_survive_a_restart(tmp_path, monkeypatch):
    """A redeploy must not cost the operator the trips into VR that settled these."""
    r = _retargeter()
    r.flip_wrist("right", "yaw")
    r.flip_wrist("left", "pitch")

    fresh = _retargeter()
    fresh._load_wrist_sign()
    assert fresh._WRIST_SIGN["right"]["yaw"] == -1.0
    assert fresh._WRIST_SIGN["left"]["pitch"] == -1.0
    assert fresh._WRIST_SIGN["left"]["roll"] == 1.0


def test_a_corrupt_sign_file_leaves_the_defaults_alone():
    r = _retargeter()
    import json as _j
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        _j.dump({"signs": {"left": {"roll": 7.0}, "nonsense": {"yaw": -1}}}, f)
        path = f.name
    type(r)._SIGN_FILE, saved = path, type(r)._SIGN_FILE
    try:
        r._load_wrist_sign()
        assert r._WRIST_SIGN["left"]["roll"] == 1.0, "a bogus value was accepted"
    finally:
        type(r)._SIGN_FILE = saved
        os.unlink(path)


def test_every_wrist_output_is_inside_the_joint_limits():
    r = _retargeter()
    import retarget_g1
    rng = np.random.default_rng(0)
    for _ in range(200):
        q = rng.normal(size=4)
        q /= np.linalg.norm(q)
        for side in ("left", "right"):
            for n, v in r._forearm_q(side, {"quat": q.tolist()}).items():
                lo, hi = retarget_g1.ARM_JOINT_LIMITS[n]
                assert lo - 1e-9 <= v <= hi + 1e-9, f"{n}={v} outside [{lo},{hi}]"


# --------------------------------------------------------------------------- #
# human-range clamp
# --------------------------------------------------------------------------- #
def test_no_wrist_target_exceeds_human_range():
    """Anything past a human wrist is whole-arm motion leaking in.

    The mapping reads hand orientation relative to the head, and WebXR reports
    no elbow or shoulder, so turning the whole arm is indistinguishable from
    turning the wrist. With the robot's shoulder held, all of it lands on the
    wrist. Measured on hardware before this clamp: both wrist_roll targets swept
    226.0 deg, the joint's entire range.
    """
    import retarget_g1
    r = _retargeter()
    H = retarget_g1.G1Retargeter.HUMAN_WRIST
    rng = np.random.default_rng(1)
    worst = {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}
    for _ in range(400):
        q = rng.normal(size=4)
        q /= np.linalg.norm(q)
        for side in ("left", "right"):
            for n, v in r._forearm_q(side, {"quat": q.tolist()}).items():
                for axis in worst:
                    if n.endswith(f"wrist_{axis}_joint"):
                        assert abs(v) <= H[axis] + 1e-9, f"{n}={v} past human range"
                        worst[axis] = max(worst[axis], abs(v))
    # and the clamp must actually bite on this spread, or the test proves nothing
    assert any(worst[a] >= H[a] - 1e-6 for a in worst), \
        "no sample reached the clamp; this test would pass on unclamped code"


def test_the_clamp_is_tighter_than_the_robots_own_limits():
    """Otherwise it is decoration -- the joint limits would already do it."""
    import retarget_g1
    H = retarget_g1.G1Retargeter.HUMAN_WRIST
    for side in ("left", "right"):
        for axis, h in H.items():
            lo, hi = retarget_g1.ARM_JOINT_LIMITS[f"{side}_wrist_{axis}_joint"]
            assert h < max(abs(lo), abs(hi)), \
                f"{side} {axis}: human {h} does not constrain robot [{lo},{hi}]"


def test_small_motions_are_untouched_by_the_clamp():
    """It must not quietly reshape ordinary teleop."""
    import retarget_g1
    r = _retargeter()
    r._wrist_zero("left", np.eye(3))
    q = _quat(np.array([0.0, 1.0, 0.0]), np.radians(20))
    out = r._forearm_q("left", {"quat": q.tolist()})
    H = retarget_g1.G1Retargeter.HUMAN_WRIST
    for axis in H:
        v = out[f"left_wrist_{axis}_joint"]
        assert abs(v) < H[axis] - 1e-6, f"a 20 deg input hit the {axis} clamp"


# --------------------------------------------------------------------------- #
# impossible-step gate
# --------------------------------------------------------------------------- #
def test_an_impossible_step_holds_the_last_good_angle():
    """The 169 deg single-frame teleport this exists for.

    Driven through _degap directly. Going via a quaternion would prove less
    than it looks: the frame rotation redistributes a big input across all
    three axes, and the human-range clamp trims it further, so the step that
    actually reaches the gate is nothing like the one handed in.
    """
    r = _retargeter()
    assert r._degap("left", "roll", 0.10) == pytest.approx(0.10)
    # +84.8 -> -84.8 deg, the measured signature
    assert r._degap("left", "roll", -1.48) == pytest.approx(0.10), \
        "a teleport reached the robot"
    assert r.n_wrist_gaps == 1
    # ...and recovery: once the signal is sane again it is followed.
    assert r._degap("left", "roll", 0.25) == pytest.approx(0.25)


def test_the_gate_holds_rather_than_clamping():
    """Holding says 'the operator did not move', which is true for a few frames.

    Clamping to the step limit would instead assert a move they never made, and
    a burst of those walks the wrist somewhere real.
    """
    r = _retargeter()
    r._degap("left", "roll", 0.0)
    out = r._degap("left", "roll", 3.0)
    assert out == pytest.approx(0.0), f"gate clamped to {out} instead of holding"


def test_ordinary_fast_motion_passes_through():
    """It must reject teleports, not brisk teleop."""
    import retarget_g1
    r = _retargeter()
    r._wrist_zero("left", np.eye(3))
    prev = None
    for deg in range(0, 61, 5):          # 5 deg per frame = 450 deg/s at 90 fps
        q = _quat(np.array([1.0, 0.0, 0.0]), np.radians(deg))
        v = r._forearm_q("left", {"quat": q.tolist()})["left_wrist_roll_joint"]
        if prev is not None:
            assert v != pytest.approx(prev), f"a legitimate 5 deg step was rejected at {deg}"
        prev = v
    assert r.n_wrist_gaps == 0, "brisk motion was treated as a teleport"


def test_the_gate_is_per_hand():
    r = _retargeter()
    for side in ("left", "right"):
        r._wrist_zero(side, np.eye(3))
        r._forearm_q(side, {"quat": _quat(np.array([1.0, 0, 0]), np.radians(10)).tolist()})
    r._forearm_q("left", {"quat": _quat(np.array([1.0, 0, 0]), np.radians(180)).tolist()})
    # the right hand must be unaffected by the left hand's rejection
    v = r._forearm_q("right", {"quat": _quat(np.array([1.0, 0, 0]), np.radians(25)).tolist()})
    assert v["right_wrist_roll_joint"] != pytest.approx(0.0, abs=1e-6)


def test_rezeroing_forgets_the_continuity_history():
    """Otherwise the first frame after a re-zero is compared to the old reference."""
    r = _retargeter()
    r._forearm_q("left", {"quat": _quat(np.array([1.0, 0, 0]), np.radians(10)).tolist()})
    r._forearm_q("left", {"quat": _quat(np.array([1.0, 0, 0]), np.radians(60)).tolist()})
    r.zero_wrists()
    assert not r._wrist_prev, "stale continuity state survived a re-zero"


def test_the_elbow_is_gated_too():
    """Found by replaying a real session: a 40.8 deg single-frame elbow jump.

    The wrist teleport comes from the Euler decomposition; the elbow's comes
    from the wrist POSITION jumping when tracking drops and reacquires. Same
    consequence downstream, so the same gate.
    """
    r = _retargeter()
    near, far = type(r)._ELBOW_CAL["left"]
    settled = r._elbow_q("left", {"wrist": _wrist_at(r, "left", near + 0.02)})
    out = r._forearm_q("left", {"quat": [0.0, 0.0, 0.0, 1.0],
                                "wrist": _wrist_at(r, "left", near + 0.02)})
    first = out["left_elbow_joint"]
    # jump to the far end: a full-span step, far past MAX_WRIST_STEP
    jumped = r._forearm_q("left", {"quat": [0.0, 0.0, 0.0, 1.0],
                                   "wrist": _wrist_at(r, "left", far)})
    assert jumped["left_elbow_joint"] == pytest.approx(first), \
        "an elbow teleport reached the robot"
    assert r.n_wrist_gaps >= 1


def test_ordinary_reaching_is_not_gated():
    r = _retargeter()
    near, far = type(r)._ELBOW_CAL["left"]
    prev = None
    for frac in np.linspace(0.0, 1.0, 40):      # gentle reach across the span
        d = near + frac * (far - near)
        v = r._forearm_q("left", {"quat": [0.0, 0.0, 0.0, 1.0],
                                  "wrist": _wrist_at(r, "left", d)})["left_elbow_joint"]
        if prev is not None and frac > 0:
            assert v != pytest.approx(prev), "a legitimate reach step was rejected"
        prev = v


def test_an_inverted_reach_calibration_is_refused():
    """Captured live, the usual failure is capturing before the operator moves.

    Stored, far <= near either disables the elbow or inverts it, so reaching
    out would FOLD the arm. Observed for real: right far came back 0.156 m
    against a near of 0.202.
    """
    r = _retargeter()
    before = dict(r._ELBOW_CAL)
    r.elbow_cal("right", "near", _wrist_at(r, "right", 0.202))
    msg = r.elbow_cal("right", "far", _wrist_at(r, "right", 0.156))
    assert msg.startswith("refused"), msg
    assert "disables the elbow" in msg
    assert r._ELBOW_CAL["right"][1] == pytest.approx(before["right"][1]), \
        "the refused capture still overwrote the calibration"


def test_a_too_tight_span_is_refused():
    r = _retargeter()
    r.elbow_cal("left", "near", _wrist_at(r, "left", 0.30))
    assert r.elbow_cal("left", "far", _wrist_at(r, "left", 0.33)).startswith("refused")


def test_a_good_span_still_stores():
    r = _retargeter()
    assert not r.elbow_cal("left", "near", _wrist_at(r, "left", 0.20)).startswith("refused")
    assert not r.elbow_cal("left", "far", _wrist_at(r, "left", 0.50)).startswith("refused")
    assert r._ELBOW_CAL["left"] == pytest.approx((0.20, 0.50), abs=1e-6)


def test_a_shoulder_frame_is_accepted_as_partial():
    """Shoulder mode sends 6 joints per side, not 4.

    Caught live: the retargeter and the step gate were updated for the
    shoulder and the sink was not, so every frame was refused as incomplete --
    12 joints at 90 fps and hands_fresh stayed False. It failed safe, which is
    why the check exists, but it failed silently from the operator's side.
    """
    import asyncio
    import sink_realg1 as sk
    s = _sink()
    six = sorted(sk.RealG1Sink._FOREARM_SET["left"] | sk.RealG1Sink._SHOULDER_SET["left"])
    asyncio.run(s.apply(_targets(six)))
    assert s._latest is not None, "a legitimate shoulder frame was dropped"
    assert set(s._latest["arm_partial"]) == set(six)


def test_both_hands_with_shoulders_are_accepted():
    import asyncio
    import sink_realg1 as sk
    s = _sink()
    names = sorted(set().union(*(sk.RealG1Sink._FOREARM_SET[x]
                                 | sk.RealG1Sink._SHOULDER_SET[x]
                                 for x in ("left", "right"))))
    asyncio.run(s.apply(_targets(names)))
    # Seven a side is every arm joint: the sink sends it as a complete arm.
    assert s._latest is not None and len(s._latest["arm"]) == 14


def test_forearm_only_frames_still_work_with_shoulder_mode_off():
    import asyncio
    import sink_realg1 as sk
    s = _sink()
    asyncio.run(s.apply(_targets(sorted(sk.RealG1Sink._FOREARM_SET["right"]))))
    assert s._latest is not None


def test_a_frame_missing_one_shoulder_joint_is_refused():
    """Five of a side's six is a frame that lost one."""
    import asyncio
    import sink_realg1 as sk
    s = _sink()
    six = sorted(sk.RealG1Sink._FOREARM_SET["left"] | sk.RealG1Sink._SHOULDER_SET["left"])
    asyncio.run(s.apply(_targets(six[:-1])))
    assert s._latest is None, "an incomplete shoulder frame was accepted"


def test_a_stray_yaw_on_a_forearm_only_side_is_refused():
    """Yaw travels only as part of the whole seven-joint shoulder shape. Four
    forearm joints plus a lone yaw is neither legal shape."""
    import asyncio
    import sink_realg1 as sk
    s = _sink()
    four = sorted(sk.RealG1Sink._FOREARM_SET["left"])
    asyncio.run(s.apply(_targets(four + ["left_shoulder_yaw_joint"])))
    assert s._latest is None


# --------------------------------------------------------------------------- #
# the gate must yield to persistent motion
# --------------------------------------------------------------------------- #
def test_a_sustained_jump_is_eventually_believed():
    """The bug this exists for: a dropout froze a joint for the whole session.

    _degap returned the last good value without UPDATING it, so every later
    frame was compared against the same stale number and rejected for ever.
    Reproduced: 200 frames after a 1.0 rad jump the joint still read 0.0. A
    recorded session shows a left elbow frozen for 4.3 s and a right wrist yaw
    frozen for the rest of the run.
    """
    r = _retargeter()
    r._degap("left", "roll", 0.0)
    out = [r._degap("left", "roll", 1.0) for _ in range(10)]
    assert out[0] == pytest.approx(0.0), "the first frame of a jump should be held"
    assert out[-1] == pytest.approx(1.0), "the joint never caught up: still frozen"
    assert r.n_wrist_jumps == 1, "the accepted jump was not counted"


def test_a_single_frame_glitch_is_still_rejected():
    """Yielding must not cost the thing the gate was built for."""
    r = _retargeter()
    r._degap("left", "roll", 0.0)
    assert r._degap("left", "roll", 3.0) == pytest.approx(0.0)
    assert r._degap("left", "roll", 0.02) == pytest.approx(0.02)
    assert r.n_wrist_gaps == 1 and r.n_wrist_jumps == 0


def test_the_run_counter_resets_on_a_good_frame():
    """Two glitches far apart must not add up to an accepted jump."""
    r = _retargeter()
    r._degap("left", "roll", 0.0)
    for _ in range(4):
        r._degap("left", "roll", 3.0)     # glitch
        r._degap("left", "roll", 0.0)     # back to normal
    assert r.n_wrist_jumps == 0, "isolated glitches accumulated into an accept"


def test_yielding_is_per_axis_and_per_hand():
    r = _retargeter()
    for side in ("left", "right"):
        for ax in ("roll", "pitch"):
            r._degap(side, ax, 0.0)
    for _ in range(5):
        r._degap("left", "roll", 1.0)
    # only the axis that actually jumped should have moved
    assert r._degap("left", "roll", 1.0) == pytest.approx(1.0)
    assert r._degap("left", "pitch", 0.0) == pytest.approx(0.0)
    assert r._degap("right", "roll", 0.0) == pytest.approx(0.0)


def test_zeroing_clears_the_run_counters_too():
    r = _retargeter()
    r._degap("left", "roll", 0.0)
    r._degap("left", "roll", 3.0)
    r.zero_wrists()
    assert not r._gap_runs, "stale rejection counts survived a re-zero"


# --------------------------------------------------------------------------- #
# The held pose must be the pose the arm is ACTUALLY at, not the pose control
# was taken at.
#
# _hold_pose was captured once, in take_control, and never again. Every path
# that moves the arm afterwards -- a watchdog freeze, hold(), go_home(), limp
# then hold -- left the arm somewhere else while merge_partial_arm kept laying
# forearm frames over the stale pose. Sequence on hardware: take_control ->
# home -> arm in forearm mode -> the unnamed shoulders are commanded back to
# the take_control pose, at slew speed, under the leash. A move nobody asked
# for, at the moment of arming.
# --------------------------------------------------------------------------- #

class _TrackingArm:
    """Fake arm whose measured pose follows the last command."""

    def __init__(self, q):
        self.q = list(q)
        self.commands = []

    def ctrl_dual_arm(self, q, dq):
        self.commands.append(list(q))
        self.q = list(q)

    def get_current_dual_arm_q(self):
        return list(self.q)

    def set_arm_compliance(self, kp=None, kd=None):
        pass


MOVED = [round(0.05 + 0.01 * i, 3) for i in range(14)]


def _held_cmd(arm_q):
    c = ob.Commander()
    c.arm = _TrackingArm(arm_q)
    c._hold_pose = list(HOLD)
    c.measured_arm_q = list(arm_q)
    c.state = "HOLDING"
    return c


def test_a_freeze_refreshes_the_held_pose():
    c = _held_cmd(MOVED)                    # the arm has moved since take_control
    with c.lock:
        c._freeze()
    out = c.merge_partial_arm({"left_elbow_joint": 0.5})
    i_el = ob.ARM_JOINT_INDEX["left_elbow_joint"]
    for i, v in enumerate(ob.sanitize_arm(MOVED)):
        if i != i_el:
            assert out[i] == pytest.approx(v), (
                f"joint {i} laid over the take_control pose, not where the arm froze")


def test_hold_refreshes_the_held_pose():
    c = _held_cmd(MOVED)
    c.hand = None
    assert c.hold().startswith("HOLDING")
    assert c._hold_pose == pytest.approx(MOVED)


def test_go_home_refreshes_the_held_pose():
    c = _held_cmd(HOLD)
    c.home_q = list(MOVED)
    # The reply is not the subject here, and this fixture's measured joints
    # never move, so go_home correctly reports that the arm did not arrive.
    # What is under test is that _hold_pose follows the COMMAND.
    c.go_home()
    assert c._hold_pose == pytest.approx(MOVED), (
        "after homing, a forearm frame would drag the shoulders back to the "
        "take_control pose")
    out = c.merge_partial_arm({})
    assert out == pytest.approx(ob.sanitize_arm(MOVED))
