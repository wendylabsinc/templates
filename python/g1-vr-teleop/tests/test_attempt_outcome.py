"""An attempt ends with a verdict, or the recording is a movement log.

Marks (#20) say where an attempt starts. Nothing says how it ended: the can
sessions of 2026-09-15 hold seventeen sustained grips and the only record of
which ones lifted the can is the operator's memory. For training the outcome
IS the label -- a reach that missed and a reach that lifted look alike in the
joints for their first two seconds -- so the operator presses success or fail
right after letting go, and the recorder writes an outcome row in place. The
phase names the furthest point reached, from the coke demo's lifecycle. Replay
and the validator attach each outcome to its attempt and count them.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "teleop", "bridge"))

import episode_check  # noqa: E402
import server  # noqa: E402
from session_recorder import SessionRecorder, PHASES, RESULTS  # noqa: E402
from tests.test_endpoint_gating import _req, KEY  # noqa: E402


def _drain(r, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not r.status()["queued"]:
            return True
        time.sleep(0.01)
    return False


def _rows(path):
    return [json.loads(l) for l in open(path) if l.strip()]


# ---- the vocabulary -----------------------------------------------------------

def test_the_lifecycle_vocabulary_is_the_coke_demo_sequence():
    """rest -> approach -> pregrasp -> grasp -> lift -> present -> clear -> place
    -> rest -> release, as the g1-coke-demo README defines the operator task."""
    assert PHASES == ("rest", "approach", "pregrasp", "grasp", "lift", "present",
                      "clear", "place", "release")
    assert RESULTS == ("success", "fail", "abort")


# ---- recorder -----------------------------------------------------------------

def test_an_outcome_is_written_in_order_and_counted(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    sid = rec.start("x")["session"]
    rec.mark("attempt 1")
    rec.note({"t": 1.0, "hands": []}, {"left_elbow_joint": 0.1}, {"state": "ARMED"})
    rec.note({"t": 1.1, "hands": []}, {"left_elbow_joint": 0.2}, {"state": "ARMED"})
    out = rec.outcome("success", "lift", "clean")
    assert out == {"ok": True, "result": "success", "phase": "lift", "note": "clean",
                   "outcomes": {"success": 1, "fail": 0, "abort": 0}}
    rec.note({"t": 1.2, "hands": []}, {"left_elbow_joint": 0.2}, {"state": "ARMED"})
    rec.outcome("fail", "grasp")
    assert _drain(rec)
    stop = rec.stop()
    rows = _rows(tmp_path / f"{sid}.jsonl")
    types = [r.get("type", "frame") for r in rows]
    assert types == ["header", "mark", "frame", "frame", "outcome", "frame", "outcome", "footer"], types
    o = rows[4]
    assert o["result"] == "success" and o["phase"] == "lift" and o["note"] == "clean"
    assert isinstance(o["t"], float)
    assert rows[6]["result"] == "fail" and rows[6]["phase"] == "grasp" and rows[6]["note"] == ""
    assert rows[-1]["outcomes"] == {"success": 1, "fail": 1, "abort": 0}
    assert stop["outcomes"] == {"success": 1, "fail": 1, "abort": 0}
    assert rows[-1]["frames"] == 3, "an outcome is not a frame"


def test_a_bad_result_or_phase_is_refused_and_not_written(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    sid = rec.start("x")["session"]
    assert rec.outcome("won")["ok"] is False
    assert rec.outcome("success", "flying")["ok"] is False
    assert rec.outcome("", "grasp")["ok"] is False
    assert _drain(rec)
    rec.stop()
    rows = _rows(tmp_path / f"{sid}.jsonl")
    assert [r["type"] for r in rows] == ["header", "footer"]
    assert rows[-1]["outcomes"] == {"success": 0, "fail": 0, "abort": 0}


def test_the_phase_may_be_empty_and_the_note_is_bounded(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    sid = rec.start("x")["session"]
    assert rec.outcome("abort", "", "x" * 500)["ok"] is True
    assert _drain(rec)
    rec.stop()
    rows = _rows(tmp_path / f"{sid}.jsonl")
    assert rows[1]["phase"] == "" and len(rows[1]["note"]) == 200


def test_an_outcome_outside_a_session_is_refused_not_written(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    assert rec.outcome("success")["ok"] is False
    assert not list(tmp_path.iterdir())


def test_a_new_session_starts_its_counts_at_zero(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    rec.start("a")
    rec.outcome("success")
    assert _drain(rec)
    rec.stop()
    rec.start("b")
    assert _drain(rec)
    assert rec.stop()["outcomes"] == {"success": 0, "fail": 0, "abort": 0}


# ---- the ways to record one ---------------------------------------------------

class _Rec:
    def __init__(self):
        self.outcomes = []

    def outcome(self, result, phase="", note=""):
        self.outcomes.append((result, phase, note))
        return {"ok": True, "result": result, "phase": phase, "note": note,
                "outcomes": {"success": 0, "fail": 0, "abort": 0}}


def test_post_session_outcome_is_gone_and_does_not_record():
    rec = _Rec()
    app = {"control_key": KEY, "session": rec, "retargeter": object(), "stats": {}, "sink": server.LogSink()}
    resp = asyncio.run(server.session_handler(
        _req(app, {"step": "outcome"}, key=KEY, query={"result": "success", "phase": "place", "note": "hoop"})))
    assert resp.status == 410 and json.loads(resp.text)["ok"] is False
    assert rec.outcomes == []


def test_post_session_outcome_never_reaches_the_recorder():
    class _No:
        def outcome(self, *a, **k):
            return {"ok": False, "error": "not recording"}
    app = {"control_key": KEY, "session": _No(), "retargeter": object(), "stats": {}, "sink": server.LogSink()}
    resp = asyncio.run(server.session_handler(_req(app, {"step": "outcome"}, key=KEY, query={"result": "success"})))
    assert resp.status == 410


def test_the_primary_face_buttons_do_not_record_outcomes():
    rec = _Rec()
    stats = {}

    def frame(left_primary, right_primary):
        return {"hands": [
            {"hand": "left", "source": "controller", "buttons": {"primary": left_primary, "secondary": False}},
            {"hand": "right", "source": "controller", "buttons": {"primary": right_primary, "secondary": False}}]}
    app = {"source": "xr", "session": rec}
    for f in (frame(False, True), frame(True, False)):
        server.headset_buttons(f, stats, app)
    assert rec.outcomes == []


def test_the_control_page_has_no_manual_outcome_controls():
    html = open(os.path.join(ROOT, "teleop", "web", "control.html")).read()
    assert 'data-outcome=' not in html
    assert 'id="phase"' not in html
    assert "/session/outcome" not in html


# ---- attribution to attempts -------------------------------------------------

def _frame(t, armed=True):
    return {"t": t, "hands": [], "head": None, "robot": {"state": "ARMED" if armed else "HOLDING", "q": [0.0] * 14,
                                                       "q_age_s": 0.01, "applied": True},
            "targets": {"left_elbow_joint": 0.1, "left_wrist_roll_joint": 0.0}}


def _session_with_outcomes(tmp_path):
    """Three attempts. #1 gets its verdict INSIDE its span; #2's verdict comes
    0.4 s after its last frame, during the pause before #3 (the operator lets
    go, then presses); #3 gets none."""
    sid = "20260916-000000-o"
    p = tmp_path / f"{sid}.jsonl"
    with open(p, "w") as fh:
        fh.write(json.dumps({"type": "header", "session": sid, "t_start": 100.0, "v": 1}) + "\n")
        t = 100.0
        fh.write(json.dumps({"type": "mark", "t": t, "label": "attempt 1"}) + "\n")
        for i in range(90):
            fh.write(json.dumps(_frame(t + i / 90)) + "\n")
            if i == 60:
                fh.write(json.dumps({"type": "outcome", "t": t + i / 90 + 0.001,
                                     "result": "success", "phase": "release", "note": ""}) + "\n")
        t += 1.0
        fh.write(json.dumps({"type": "mark", "t": t, "label": "attempt 2"}) + "\n")
        for i in range(180):
            fh.write(json.dumps(_frame(t + i / 90)) + "\n")
        t += 2.0
        fh.write(json.dumps({"type": "outcome", "t": t + 0.4, "result": "fail", "phase": "grasp",
                             "note": "slipped"}) + "\n")
        t += 5.0                                     # a pause, no mark
        for i in range(45):
            fh.write(json.dumps(_frame(t + i / 90, armed=False)) + "\n")
        fh.write(json.dumps({"type": "footer", "frames": 315, "dropped": 0, "marks": 2,
                             "outcomes": {"success": 1, "fail": 1, "abort": 0}, "t_end": t + 1}) + "\n")
    return str(p)


def test_the_validator_attaches_each_outcome_to_its_attempt(tmp_path):
    rep = episode_check.check(_session_with_outcomes(tmp_path))
    assert rep["frames"] == 315, "outcome rows are not frames"
    assert rep["outcomes"] == {"success": 1, "fail": 1, "abort": 0}
    att = rep["attempt_list"]
    assert [a["label"] for a in att] == ["attempt 1", "attempt 2", "after pause"]
    assert att[0]["outcome"] == {"result": "success", "phase": "release"}
    assert att[1]["outcome"] == {"result": "fail", "phase": "grasp"}, \
        "a verdict pressed just after letting go belongs to the attempt that just ended"
    assert att[2]["outcome"] is None
    codes = {i["code"]: i for i in rep["issues"]}
    assert "footer_mismatch" not in codes
    assert codes["no_outcome"]["severity"] == "warn"
    assert "1 attempt" in codes["no_outcome"]["detail"]


def test_a_session_with_no_outcomes_at_all_is_not_warned(tmp_path):
    """A calibration run or a tracking test was never being labelled. The
    zero counts in the summary say so; a warning would flag every session
    recorded before this feature existed."""
    sid = "20260916-000002-o"
    p = tmp_path / f"{sid}.jsonl"
    with open(p, "w") as fh:
        fh.write(json.dumps({"type": "header", "session": sid, "t_start": 100.0, "v": 1}) + "\n")
        for i in range(90):
            fh.write(json.dumps(_frame(100.0 + i / 90)) + "\n")
        fh.write(json.dumps({"type": "footer", "frames": 90, "dropped": 0, "marks": 0, "t_end": 101.3}) + "\n")
    rep = episode_check.check(str(p))
    assert "no_outcome" not in {i["code"] for i in rep["issues"]}
    assert rep["outcomes"] == {"success": 0, "fail": 0, "abort": 0}
    assert rep["attempt_list"][0]["outcome"] is None


def test_a_session_where_every_attempt_has_a_verdict_raises_no_outcome_warning(tmp_path):
    sid = "20260916-000001-o"
    p = tmp_path / f"{sid}.jsonl"
    with open(p, "w") as fh:
        fh.write(json.dumps({"type": "header", "session": sid, "t_start": 100.0, "v": 1}) + "\n")
        for i in range(90):
            fh.write(json.dumps(_frame(100.0 + i / 90)) + "\n")
        fh.write(json.dumps({"type": "outcome", "t": 101.2, "result": "success", "phase": "", "note": ""}) + "\n")
        fh.write(json.dumps({"type": "footer", "frames": 90, "dropped": 0, "marks": 0,
                             "outcomes": {"success": 1, "fail": 0, "abort": 0}, "t_end": 101.3}) + "\n")
    rep = episode_check.check(str(p))
    assert "no_outcome" not in {i["code"] for i in rep["issues"]}
    assert rep["attempt_list"][0]["outcome"] == {"result": "success", "phase": ""}
    assert "success 1" in episode_check.summary(rep)

