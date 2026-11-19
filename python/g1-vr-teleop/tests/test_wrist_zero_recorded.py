"""The wrist zero must be in the recording, or the wrist joints cannot be replayed.

Found on 20260915-001254-first-full-stack.jsonl: replayed through the same code,
every shoulder and elbow target matched to the digit and every wrist joint
differed, by up to 170 deg on wrist roll. The wrist joints are RELATIVE to the
pose the operator zeroed at, that pose was never written down, and the replay
could only re-zero on its first frame. The recording had raised exactly the
question it then could not answer: was the left wrist yaw pinned at +35 deg for
47 % of the run because of a bias, or because of the zero pose?

These tests pin the fix: the retargeter exposes its reference, the recorder
writes it on the first frame and on every change, and the server passes it
through -- so an offline replay of a session with a mid-run re-zero can
reproduce its wrist joints exactly.
"""
import json
import math
import pathlib
import sys
import time

import numpy as np
import pytest

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))

import retarget_g1 as rg  # noqa: E402
from session_recorder import SessionRecorder  # noqa: E402


# ----------------------------------------------------------------- helpers
def _quat(axis, ang):
    axis = np.asarray(axis, float)
    axis = axis / np.linalg.norm(axis)
    s = math.sin(ang / 2)
    return [axis[0] * s, axis[1] * s, axis[2] * s, math.cos(ang / 2)]   # x, y, z, w


def _hand(side, i):
    """A hand that turns and reaches a little every frame, so nothing is static."""
    sgn = -1.0 if side == "left" else 1.0
    ang = 0.35 * math.sin(i / 17.0) + 0.2 * math.sin(i / 5.0)
    quat = _quat([0.3, sgn * 0.7, -0.2], ang)
    wrist = [sgn * (0.22 + 0.05 * math.sin(i / 11.0)), -0.15 + 0.08 * math.sin(i / 13.0),
             -0.35 - 0.06 * math.cos(i / 9.0)]
    lm = np.zeros((25, 3)); lm[:, 0] = np.linspace(0, 0.1, 25) * sgn
    return {"hand": side, "landmarks": lm.reshape(-1).tolist(), "wrist": wrist,
            "quat": quat, "fabricated": 0}


def _frames(n):
    return [{"hands": [_hand("left", i), _hand("right", i)],
             "head": {"pos": [0, 1.6, 0], "quat": [0, 0, 0, 1]}} for i in range(n)]


def _fresh_retargeter(shoulder=True):
    """A geometry-path retargeter without the heavy constructor."""
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    r.forearm, r.shoulder, r.wrist_fk = True, shoulder, shoulder
    r._wrist_ref, r._forearm_ref, r._wrist_prev = {}, {}, {}
    # The calibrated limits, as server.py installs them.
    r.ROBOT_LIMITS = rg.ROBOT_LIMITS
    return r


def _solve(r, row, shoulder=True):
    """One recorded frame through the arm path. -> {joint: angle}."""
    hands = {h.get("hand"): h for h in (row.get("hands") or ())}
    fr = {}
    for side in ("left", "right"):
        h = hands.get(side)
        if h and "quat" in h:
            sh = r._shoulder_q(side, h) if shoulder else {}
            pr = ((sh[f"{side}_shoulder_pitch_joint"],
                   sh[f"{side}_shoulder_roll_joint"],
                   sh.get(f"{side}_shoulder_yaw_joint", 0.0)) if sh else None)
            fr.update(r._forearm_q(side, h, shoulder=pr))
            fr.update(sh)
    return fr


def _wrist_diff_deg(rows, fresh):
    worst = 0.0
    for row, f in zip(rows, fresh):
        for k, v in row["targets"].items():
            if "wrist" in k and k in f:
                worst = max(worst, math.degrees(abs(v - f[k])))
    return worst


@pytest.fixture
def rec(tmp_path):
    r = SessionRecorder(str(tmp_path / "sessions"))
    yield r
    r.close()


# ----------------------------------------------------------------- retargeter
def test_the_retargeter_exposes_its_reference_and_versions_it():
    r = _fresh_retargeter(shoulder=False)
    assert r.wrist_reference() == {"v": 0, "sides": {}}
    _solve(r, _frames(1)[0], shoulder=False)
    ref = r.wrist_reference()
    assert set(ref["sides"]) == {"left", "right"}
    assert ref["v"] >= 1
    assert np.asarray(ref["sides"]["right"]["R0"]).shape == (3, 3)
    v = ref["v"]
    _solve(r, _frames(2)[1], shoulder=False)
    assert r.wrist_reference()["v"] == v, "a frame that captures nothing must not bump the version"
    r.zero_wrists()
    assert r.wrist_reference()["v"] > v, "clearing the zero is a change"


def test_a_reference_can_be_installed_and_is_then_not_recaptured():
    a = _fresh_retargeter()
    f = _frames(3)
    _solve(a, f[0]); _solve(a, f[1])
    ref = a.wrist_reference()
    b = _fresh_retargeter()
    b.set_wrist_reference(ref)
    for side in ("left", "right"):
        np.testing.assert_array_equal(b._wrist_ref[side], np.asarray(ref["sides"][side]["R0"]))
        np.testing.assert_array_equal(b._forearm_ref[side], np.asarray(ref["sides"][side]["F0"]))
    v = b.wrist_reference()["v"]
    _solve(b, f[2])
    assert b.wrist_reference()["v"] == v, "installing a reference must stop the lazy first-frame capture"


def test_a_side_without_a_forearm_zero_round_trips_as_none():
    r = _fresh_retargeter(shoulder=False)   # constant path: R0 only, no F0
    _solve(r, _frames(1)[0], shoulder=False)
    ref = r.wrist_reference()
    assert ref["sides"]["left"]["F0"] is None
    b = _fresh_retargeter(shoulder=False)
    b.set_wrist_reference(ref)
    assert "left" in b._wrist_ref and "left" not in b._forearm_ref


# ----------------------------------------------------------------- recorder
def test_the_first_frame_carries_the_reference_and_only_changes_repeat_it(rec):
    sid = rec.start()["session"]
    ref1 = {"v": 1, "sides": {"right": {"R0": np.eye(3).tolist(), "F0": None}}}
    ref2 = {"v": 2, "sides": {"right": {"R0": (-np.eye(3)).tolist(), "F0": None}}}
    f = _frames(1)[0]
    rec.note(f, {"right_elbow_joint": 0.1}, None, wrist_ref=ref1)
    rec.note(f, {"right_elbow_joint": 0.2}, None, wrist_ref=ref1)   # same zero: not repeated
    rec.note(f, {"right_elbow_joint": 0.3}, None, wrist_ref=ref2)   # new zero: written
    rec.note(f, {"right_elbow_joint": 0.4}, None, wrist_ref=ref2)
    t0 = time.time()
    while rec.status()["queued"] and time.time() - t0 < 5:
        time.sleep(0.01)
    rec.stop()
    with open(rec.path(sid)) as fh:
        body = [json.loads(l) for l in fh if l.strip()][1:-1]
    assert [("wrist_ref" in r) for r in body] == [True, False, True, False]
    assert body[0]["wrist_ref"] == ref1 and body[2]["wrist_ref"] == ref2


def test_a_new_session_writes_the_reference_again_even_if_unchanged(rec):
    ref = {"v": 7, "sides": {}}
    f = _frames(1)[0]
    s1 = rec.start()["session"]; rec.note(f, None, None, wrist_ref=ref)
    t0 = time.time()
    while rec.status()["queued"] and time.time() - t0 < 5:
        time.sleep(0.01)
    rec.stop()
    s2 = rec.start()["session"]; rec.note(f, None, None, wrist_ref=ref)
    t0 = time.time()
    while rec.status()["queued"] and time.time() - t0 < 5:
        time.sleep(0.01)
    rec.stop()
    for sid in (s1, s2):
        with open(rec.path(sid)) as fh:
            body = [json.loads(l) for l in fh if l.strip()][1:-1]
        assert "wrist_ref" in body[0], "each file must stand alone"


def test_frames_without_a_reference_still_record(rec):
    sid = rec.start()["session"]
    rec.note(_frames(1)[0], {"left_elbow_joint": 0.5})
    t0 = time.time()
    while rec.status()["queued"] and time.time() - t0 < 5:
        time.sleep(0.01)
    rec.stop()
    with open(rec.path(sid)) as fh:
        body = [json.loads(l) for l in fh if l.strip()][1:-1]
    assert body[0]["targets"]["left_elbow_joint"] == pytest.approx(0.5)
    assert "wrist_ref" not in body[0]


# ----------------------------------------------------------------- server wiring
def test_the_server_passes_the_reference_to_the_recorder():
    body = (ROOT / "teleop" / "bridge" / "server.py").read_text()
    drain = body.split("async def drain")[1].split("drain_task")[0]
    assert "session.note(" in drain
    assert "wrist_ref=" in drain, "the frame path records without the wrist zero again"
    assert "wrist_reference()" in drain
