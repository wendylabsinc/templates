"""One file, one attempt -- or at least one file that says where each attempt is.

The real can sessions are one recording per several attempts, with an 85 s
pause inside one of them and no marks. For training, an attempt is the unit
(start pose, reach, grasp, handover, release, back home), and today the only
way to cut them apart is by eye. So: the operator drops a marker -- a button on
the control page, a face button on the controller, or POST /session/mark --
and the recorder writes a mark row in place. Replay and the validator split a
session into attempts at marks and at pauses over a second, and report each.
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
from session_recorder import SessionRecorder  # noqa: E402
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


# ---- recorder ---------------------------------------------------------------

def test_a_mark_is_written_in_order_between_the_frames(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    sid = rec.start("x")["session"]
    rec.note({"t": 1.0, "hands": []}, {"left_elbow_joint": 0.1}, {"state": "ARMED"})
    rec.mark("attempt 1 start")
    rec.note({"t": 1.1, "hands": []}, {"left_elbow_joint": 0.2}, {"state": "ARMED"})
    assert _drain(rec)
    out = rec.stop()
    rows = _rows(tmp_path / f"{sid}.jsonl")
    types = [r.get("type", "frame") for r in rows]
    assert types == ["header", "frame", "mark", "frame", "footer"], types
    assert rows[2]["label"] == "attempt 1 start" and isinstance(rows[2]["t"], float)
    assert rows[-1]["marks"] == 1 and out["marks"] == 1
    assert rows[-1]["frames"] == 2, "a mark is not a frame"


def test_a_mark_outside_a_session_is_refused_not_written(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    assert rec.mark("x")["ok"] is False
    assert not list(tmp_path.iterdir())


# ---- the ways to drop one ---------------------------------------------------

class _Rec:
    def __init__(self):
        self.marks = []

    def mark(self, label=""):
        self.marks.append(label)
        return {"ok": True, "label": label, "marks": len(self.marks)}


def test_post_session_mark_is_gone_and_does_not_mark():
    rec = _Rec()
    app = {"control_key": KEY, "session": rec, "retargeter": object(), "stats": {}, "sink": server.LogSink()}
    resp = asyncio.run(server.session_handler(_req(app, {"step": "mark"}, key=KEY, query={"label": "grasp"})))
    assert resp.status == 410
    assert json.loads(resp.text)["ok"] is False and rec.marks == []


def test_controller_face_buttons_do_not_mark():
    rec = _Rec()
    stats = {}

    def frame(pressed_left, pressed_right):
        return {"hands": [
            {"hand": "left", "source": "controller", "buttons": {"primary": False, "secondary": pressed_left}},
            {"hand": "right", "source": "controller", "buttons": {"primary": False, "secondary": pressed_right}}]}
    app = {"source": "xr", "session": rec}
    for f in (frame(False, True), frame(True, False)):
        server.headset_buttons(f, stats, app)
    assert rec.marks == []


def test_the_control_page_only_has_start_and_stop_session_buttons():
    html = open(os.path.join(ROOT, "teleop", "web", "control.html")).read()
    for step in ("start", "stop"):
        assert f'data-session="{step}"' in html, f"no {step} button on the control page"
    assert 'data-session="mark"' not in html
    assert "/session/" in html


# ---- splitting into attempts ----------------------------------------------------

def _frame(t, armed=True):
    return {"t": t, "hands": [], "head": None, "robot": {"state": "ARMED" if armed else "HOLDING", "q": [0.0] * 14,
                                                       "q_age_s": 0.01, "applied": True},
            "targets": {"left_elbow_joint": 0.1, "left_wrist_roll_joint": 0.0}}


def _session_with_marks(tmp_path):
    sid = "20260916-000000-m"
    p = tmp_path / f"{sid}.jsonl"
    with open(p, "w") as fh:
        fh.write(json.dumps({"type": "header", "session": sid, "t_start": 100.0, "v": 1}) + "\n")
        t = 100.0
        fh.write(json.dumps({"type": "mark", "t": t, "label": "attempt 1"}) + "\n")
        for i in range(90):                         # 1 s of frames
            fh.write(json.dumps(_frame(t + i / 90)) + "\n")
        t += 1.0
        fh.write(json.dumps({"type": "mark", "t": t, "label": "attempt 2"}) + "\n")
        for i in range(180):                        # 2 s
            fh.write(json.dumps(_frame(t + i / 90)) + "\n")
        t += 2.0 + 5.0                              # a 5 s pause, no mark
        for i in range(45):                         # 0.5 s
            fh.write(json.dumps(_frame(t + i / 90, armed=False)) + "\n")
        fh.write(json.dumps({"type": "footer", "frames": 315, "dropped": 0, "marks": 2, "t_end": t + 1}) + "\n")
    return str(p)


def test_attempts_split_at_marks_and_at_pauses(tmp_path):
    header, frames, footer, marks, _ = episode_check.load(_session_with_marks(tmp_path))
    assert [m["label"] for m in marks] == ["attempt 1", "attempt 2"]
    att = episode_check.attempts(frames, marks, gap_s=1.0)
    assert [a["label"] for a in att] == ["attempt 1", "attempt 2", "after pause"]
    assert [a["frames"] for a in att] == [90, 180, 45]
    assert att[1]["armed_frac"] == pytest.approx(1.0) and att[2]["armed_frac"] == 0.0
    assert att[0]["t0"] == pytest.approx(100.0) and att[1]["seconds"] == pytest.approx(2.0, abs=0.05)


def test_the_validator_counts_attempts_and_does_not_count_marks_as_frames(tmp_path):
    rep = episode_check.check(_session_with_marks(tmp_path))
    assert rep["frames"] == 315
    assert rep["marks"] == 2
    assert rep["attempts"] == 3
    assert [a["label"] for a in rep["attempt_list"]] == ["attempt 1", "attempt 2", "after pause"]
    codes = {i["code"] for i in rep["issues"]}
    assert "footer_mismatch" not in codes, "a mark row must not be counted against the footer"
