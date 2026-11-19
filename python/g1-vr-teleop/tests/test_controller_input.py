"""Controllers as an input source, beside or instead of tracked hands.

A Quest controller's grip pose stands in for the wrist and its trigger drives
the grasp. The headset sends it in the same shape as a tracked hand with
`source: "controller"` and no landmarks, so the bridge and the recorder
treat it as a hand that happens to have buttons.

What has to hold, and is pinned here:
  - one grip mapping for the trigger
  - a controller frame drives the arm through the SAME zero-referenced wrist
    path as a hand, after a fixed grip-to-wrist alignment
  - that alignment is a proper rotation and an exact mirror between hands
  - a controller entry with landmarks: null must not reach the finger retargeter
  - the recorder keeps the controller fields
"""
import json
import math
import os
import pathlib
import sys
import time

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))

import retarget_g1 as rg  # noqa: E402
from session_recorder import SessionRecorder  # noqa: E402


# ----------------------------------------------------------------- helpers
def _quat(axis, ang):
    axis = np.asarray(axis, float); axis = axis / np.linalg.norm(axis)
    s = math.sin(ang / 2)
    return [axis[0] * s, axis[1] * s, axis[2] * s, math.cos(ang / 2)]


def _R_to_quat(R):
    """3x3 -> [x, y, z, w]. Shepperd's method, no scipy."""
    R = np.asarray(R, float)
    t = np.trace(R)
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        return [(R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s, 0.25 * s]
    i = int(np.argmax(np.diag(R)))
    if i == 0:
        s = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        return [0.25 * s, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s, (R[2, 1] - R[1, 2]) / s]
    if i == 1:
        s = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        return [(R[0, 1] + R[1, 0]) / s, 0.25 * s, (R[1, 2] + R[2, 1]) / s, (R[0, 2] - R[2, 0]) / s]
    s = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
    return [(R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, 0.25 * s, (R[1, 0] - R[0, 1]) / s]


def _controller(side, i, trigger=0.0, quat=None, squeeze=0.0):
    sgn = -1.0 if side == "left" else 1.0
    quat = quat or _quat([0.3, sgn * 0.7, -0.2], 0.3 * math.sin(i / 9.0))
    return {"hand": side, "source": "controller",
            "wrist": [sgn * (0.22 + 0.04 * math.sin(i / 7.0)), -0.15, -0.35 - 0.05 * math.cos(i / 5.0)],
            "quat": [round(v, 4) for v in quat], "landmarks": None, "fabricated": 0,
            "trigger": trigger, "squeeze": squeeze, "thumbstick": [0.0, 0.0],
            "buttons": {"primary": False, "secondary": False}}


def _rt(shoulder=True):
    """The retargeter without its heavy __init__, as the other tests build it."""
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    r.forearm, r.shoulder, r.wrist_fk = True, shoulder, shoulder
    r._wrist_ref, r._forearm_ref, r._wrist_prev, r._gap_runs = {}, {}, {}, {}
    r._last, r._cal, r._cal_rec = {}, None, None
    r.smooth_tau = 0.0
    # Unrelated geometry tests keep the legacy instantaneous trigger. Dedicated
    # tests below pin the production slew behaviour.
    r._trigger_grip = rg.TriggerGripSlew(rate_per_s=0.0)
    return r


# ----------------------------------------------------------------- grip mapping
def test_trigger_grip_slew_is_slow_and_time_based():
    s = rg.TriggerGripSlew(rate_per_s=0.5, max_dt_s=0.1)
    assert s.step("right", 0.0, now=10.0) == 0.0
    assert s.step("right", 1.0, now=10.1) == pytest.approx(0.05)
    assert s.step("right", 1.0, now=10.2) == pytest.approx(0.10)
    assert s.step("right", 0.0, now=10.3) == pytest.approx(0.05)


def test_trigger_grip_slew_keeps_the_two_hands_independent():
    s = rg.TriggerGripSlew(rate_per_s=0.5, max_dt_s=0.1)
    s.step("left", 0.0, now=1.0)
    s.step("right", 0.0, now=1.0)
    assert s.step("left", 1.0, now=1.1) == pytest.approx(0.05)
    assert s.step("right", 0.0, now=1.1) == 0.0


def test_trigger_grip_slew_caps_a_tracking_gap_instead_of_snapping():
    s = rg.TriggerGripSlew(rate_per_s=0.5, max_dt_s=0.1)
    s.step("left", 0.0, now=1.0)
    assert s.step("left", 1.0, now=20.0) == pytest.approx(0.05)


def test_first_trigger_sample_seeds_to_the_current_input_not_open():
    s = rg.TriggerGripSlew(rate_per_s=2.0)
    assert s.step("right", 0.72, now=1.0) == pytest.approx(0.72)


def test_arming_holds_the_measured_grip_until_the_trigger_moves():
    s = rg.TriggerGripSlew(rate_per_s=2.0, max_dt_s=0.1)
    s.prepare("right", current=0.68)
    assert s.step("right", 0.0, now=1.0) is None
    assert s.step("right", 0.0, now=1.1) is None
    assert s.step("right", 0.1, now=1.2) == pytest.approx(0.68)
    assert s.step("right", 0.1, now=1.3) == pytest.approx(0.48)


def test_retargeter_arming_hold_emits_no_finger_command_until_trigger_moves():
    r = _rt()
    r._trigger_grip = rg.TriggerGripSlew(rate_per_s=2.0)
    measured = {"right": rg.grip_q("right", 0.63)}
    r.prepare_controller_grips(measured)
    out = r.process({"hands": [_controller("right", 0, trigger=0.0)]})
    assert not any(n in out for n in rg.DEX3_RIGHT)


def test_grip_zero_is_an_open_hand_with_the_thumb_out():
    """Open is every curl joint at its open end, thumb_0 alone left neutral.

    It used to be [0.0] * 7. That is still what THUMB_OPEN=0 gives; the
    default changed because
    zero is the MIDDLE of thumb_1's range, so the old open hand could not
    extend its thumb -- 56 % of that joint's travel was unreachable.
    """
    for side, names in (("left", rg.DEX3_LEFT), ("right", rg.DEX3_RIGHT)):
        q = dict(zip(names, rg.grip_q(side, 0.0)))
        for n, v in q.items():
            lo, hi = rg.DEX3_RANGE[n]
            if n.endswith("thumb_0_joint"):
                assert v == 0.0, "across the palm, not a curl"
            elif n.endswith("thumb_1_joint"):
                # Open, but short of the stop: how far it MAY open is a
                # contact question ("if it opens all the way then it hits the
                # body"), so it rides the THUMB_OPEN dial. See
                # test_grip_open_pose.py.
                assert 0.0 < abs(v) < max(abs(lo), abs(hi))
            else:
                assert v in (lo, hi), f"{n} opened to {v}, neither end of [{lo}, {hi}]"


def test_grip_one_curls_every_finger_to_its_far_end_and_leaves_thumb_0_alone():
    for side, names in (("left", rg.DEX3_LEFT), ("right", rg.DEX3_RIGHT)):
        q = dict(zip(names, rg.grip_q(side, 1.0)))
        for n, v in q.items():
            lo, hi = rg.DEX3_RANGE[n]
            if n.endswith("thumb_0_joint"):
                assert v == 0.0
            else:
                assert v in (lo, hi) and abs(v) == max(abs(lo), abs(hi))


def test_grip_is_monotonic_mirrored_and_clamped():
    prev = None
    for f in (0.0, 0.25, 0.5, 0.75, 1.0):
        l, r = rg.grip_q("left", f), rg.grip_q("right", f)
        np.testing.assert_allclose(l, [-x for x in r], atol=1e-12)   # mirrored ranges
        if prev is not None:
            # SIGNED, not |value|: thumb_1 now opens on the far side of zero
            # and closes through it, so its magnitude dips on the way in
            # while the joint itself never turns around. The property is that
            # a tightening grip never reverses, and abs() only stood in for
            # that while every joint started at exactly 0.
            step = [a - b for a, b in zip(r, prev)]
            close = [c - o for c, o in zip(rg.grip_q("right", 1.0), rg.grip_q("right", 0.0))]
            assert all(d * w >= -1e-12 for d, w in zip(step, close)), \
                "a joint moved away from its closed pose as the grip tightened"
        prev = r
    assert rg.grip_q("right", 7.0) == rg.grip_q("right", 1.0)
    assert rg.grip_q("right", -3.0) == rg.grip_q("right", 0.0)
    assert rg.grip_q("right", float("nan")) == rg.grip_q("right", 0.0)
    assert rg.grip_q("right", None) == rg.grip_q("right", 0.0)


# ----------------------------------------------------------------- alignment
def test_grip_to_wrist_is_a_proper_rotation_and_an_exact_mirror():
    D = rg.G1Retargeter._GRIP_TO_WRIST
    for side in ("left", "right"):
        assert np.allclose(D[side].T @ D[side], np.eye(3))
        assert np.isclose(np.linalg.det(D[side]), 1.0)
    M = np.diag([-1.0, 1.0, 1.0])
    np.testing.assert_allclose(M @ D["right"] @ M, D["left"], atol=1e-12)


def test_grip_to_wrist_maps_the_spec_frames_onto_each_other():
    """Thumb-up fist, world F forward (-Z), U up (+Y), R right (+X).

    grip: X=R, Y=-F, Z=-U.  hand right: X=-U, Y=R, Z=-F.  hand left: X=U, Y=-R, Z=-F.
    R_hand = R_grip @ D, so D's columns are the hand axes written in grip axes.
    """
    F, U, R = np.array([0, 0, -1.0]), np.array([0, 1.0, 0]), np.array([1.0, 0, 0])
    R_grip = np.column_stack([R, -F, -U])
    hand_right = np.column_stack([-U, R, -F])
    hand_left = np.column_stack([U, -R, -F])
    D = rg.G1Retargeter._GRIP_TO_WRIST
    np.testing.assert_allclose(R_grip @ D["right"], hand_right, atol=1e-12)
    np.testing.assert_allclose(R_grip @ D["left"], hand_left, atol=1e-12)


@pytest.mark.parametrize("side", ["left", "right"])
def test_a_controller_drives_the_wrist_exactly_like_the_hand_it_stands_in_for(side):
    """Feed the hand path a wrist orientation, and the controller path the grip
    orientation of the same physical hand. Same zero, same motion, same joints."""
    D = rg.G1Retargeter._GRIP_TO_WRIST[side]
    poses = [np.eye(3)] + [rg._quat_to_R(_quat([0.2, 0.9, -0.3], a)) for a in (0.2, 0.5, -0.4, 0.8)]
    a, b = _rt(), _rt()
    wrist = [(-1 if side == "left" else 1) * 0.25, -0.1, -0.4]
    for R_h in poses:
        hand = {"hand": side, "source": "hand", "wrist": wrist, "quat": _R_to_quat(R_h)}
        ctrl = {"hand": side, "source": "controller", "wrist": wrist, "quat": _R_to_quat(R_h @ D.T)}
        sh_a, sh_b = a._shoulder_q(side, hand), b._shoulder_q(side, ctrl)
        assert sh_a == sh_b
        pr = (sh_a[f"{side}_shoulder_pitch_joint"], sh_a[f"{side}_shoulder_roll_joint"]) if sh_a else None
        qa, qb = a._forearm_q(side, hand, shoulder=pr), b._forearm_q(side, ctrl, shoulder=pr)
        for k in qa:
            assert abs(qa[k] - qb[k]) < 1e-9, (k, qa[k], qb[k])


# ----------------------------------------------------------------- process()
def test_a_controller_frame_drives_arm_and_grasp_without_touching_the_finger_retargeter():
    r = _rt()
    for i in range(5):
        out = r.process({"hands": [_controller("right", i, trigger=0.6)], "head": None})
    for j in ("right_shoulder_pitch_joint", "right_shoulder_roll_joint", "right_elbow_joint",
              "right_wrist_roll_joint", "right_wrist_pitch_joint", "right_wrist_yaw_joint"):
        assert j in out, j
    fingers = [out[n] for n in rg.DEX3_RIGHT]
    np.testing.assert_allclose(fingers, rg.grip_q("right", 0.6))
    assert r.input_sources == {"right": "controller"}


def test_trigger_zero_opens_and_one_closes_within_the_joint_range():
    r = _rt()
    o = r.process({"hands": [_controller("right", 0, trigger=0.0)]})
    c = r.process({"hands": [_controller("right", 1, trigger=1.0)]})
    assert [o[n] for n in rg.DEX3_RIGHT] == rg.grip_q("right", 0.0)
    for n in rg.DEX3_RIGHT:
        lo, hi = rg.DEX3_RANGE[n]
        assert lo - 1e-12 <= c[n] <= hi + 1e-12


def test_a_missing_trigger_means_open_not_a_crash():
    r = _rt()
    h = _controller("left", 0); del h["trigger"]
    out = r.process({"hands": [h]})
    assert [out[n] for n in rg.DEX3_LEFT] == rg.grip_q("left", 0.0)


def test_mixed_frame_tracked_left_hand_and_right_controller(monkeypatch):
    """One hand tracked, the other holding a controller: both sides drive."""
    r = _rt()
    seen = []
    monkeypatch.setattr(r, "_finger_q", lambda lm, *a: seen.append(len(lm)) or [0.1] * 7)
    for attr in ("_li", "_left", "_l_to_hw", "_rot_l"):
        setattr(r, attr, None)
    left = {"hand": "left", "source": "hand", "wrist": [-0.25, -0.1, -0.4],
            "quat": [0, 0, 0, 1], "landmarks": [0.01] * 75, "fabricated": 0}
    out = r.process({"hands": [left, _controller("right", 0, trigger=1.0)]})
    assert seen == [75], "the tracked hand went through the finger retargeter once"
    assert out["left_hand_index_0_joint"] == pytest.approx(0.1)
    assert [out[n] for n in rg.DEX3_RIGHT] == rg.grip_q("right", 1.0)
    assert "left_elbow_joint" in out and "right_elbow_joint" in out
    assert r.input_sources == {"left": "hand", "right": "controller"}


def test_finger_calibration_is_not_applied_on_top_of_a_trigger():
    r = _rt()
    # a calibration that would remap the right index badly if applied
    r._cal = {"right_hand_index_0_joint": (0.5, 0.6)}
    out = r.process({"hands": [_controller("right", 0, trigger=1.0)]})
    assert out["right_hand_index_0_joint"] == rg.grip_q("right", 1.0)[rg.DEX3_RIGHT.index("right_hand_index_0_joint")]


# ----------------------------------------------------------------- recorder
def test_the_recorder_keeps_the_controller_fields(tmp_path):
    rec = SessionRecorder(str(tmp_path / "s"))
    try:
        sid = rec.start("ctrl")["session"]
        r = _rt()
        for i in range(20):
            f = {"hands": [_controller("right", i, trigger=0.3 + 0.02 * i, squeeze=0.1)],
                 "head": {"pos": [0, 1.6, 0], "quat": [0, 0, 0, 1]}}
            rec.note(f, r.process(f), {"state": "ARMED", "q": [0.0] * 14}, wrist_ref=r.wrist_reference())
        t0 = time.time()
        while rec.status()["queued"] and time.time() - t0 < 5:
            time.sleep(0.01)
        rec.stop()
        with open(rec.path(sid)) as fh:
            rows = [json.loads(l) for l in fh if l.strip()][1:-1]
    finally:
        rec.close()
    h = rows[3]["hands"][0]
    assert h["source"] == "controller" and h["landmarks"] is None
    assert h["trigger"] == pytest.approx(0.36) and h["squeeze"] == pytest.approx(0.1)
    assert h["thumbstick"] == [0.0, 0.0] and h["buttons"] == {"primary": False, "secondary": False}


def test_the_server_reports_sources_and_logs_the_input_mode():
    body = (ROOT / "teleop" / "bridge" / "server.py").read_text()
    assert '"xr_sources": st.get("sources")' in body
    assert 'stats["sources"]' in body
    assert "input={frame.get('input'" in body
