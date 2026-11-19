"""A row must say what the robot was TOLD, not only what the operator meant.

`targets` is the retargeter's output: the operator's intent. The sink then
subtracts the arming offset (target-minus-measured on the first ARMED frame,
53.8 deg on an elbow when measured) before anything reaches the onboard. So
the command the robot received is `targets` minus a per-joint constant that
was never written down. A physics replay fed `targets` reproduces a lurch the
real robot never made, and a comparison of commanded against measured sees
the offset as a permanent error on a joint nothing is touching.

So: each row carries `sent` -- the post-offset arm command the sink queued for
this frame -- and the offset itself is written once per capture as
`arm_offset`, versioned exactly like `wrist_ref`.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "teleop", "bridge"))

import episode_check  # noqa: E402
import server  # noqa: E402
import sink_realg1 as sk  # noqa: E402
from session_recorder import SessionRecorder  # noqa: E402


def _sink(state="ARMED", q=None):
    s = sk.RealG1Sink.__new__(sk.RealG1Sink)
    s._lock = threading.Lock()
    s._latest = None
    s._latest_t = 0.0
    s.n_sent = s.n_retargeted = 0
    s.onboard_state = state
    s.onboard_q = q
    s._arm_offset = None
    s._warned_no_q = False
    return s


def _drain(r, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not r.status()["queued"]:
            return True
        time.sleep(0.01)
    return False


def _rows(path):
    return [json.loads(l) for l in open(path) if l.strip()]


# ---- the sink knows its offset ------------------------------------------------

def test_no_offset_before_arming():
    s = _sink(state="HOLDING", q=[0.0] * 14)
    assert s.arm_offset() is None


def test_the_offset_is_reported_with_a_version_and_a_capture_time():
    q = [0.0] * 14
    q[sk.RealG1Sink._ARM_INDEX["left_elbow_joint"]] = 0.80
    s = _sink(q=q)
    t0 = time.time()
    s._offset_to_current_pose({"left_elbow_joint": -0.14})
    off = s.arm_offset()
    assert off["v"] == 1
    assert off["offset"]["left_elbow_joint"] == pytest.approx(-0.94)
    assert off["captured_at"] >= t0


def test_re_arming_captures_a_new_offset_with_a_new_version():
    """Each arming starts from wherever the arm is THEN, and the recorder
    must be able to tell the second capture from the first."""
    q = [0.0] * 14
    s = _sink(q=q)
    s._offset_to_current_pose({"left_elbow_joint": 0.5})
    assert s.arm_offset()["v"] == 1
    s.onboard_state = "HOLDING"
    s._offset_to_current_pose({"left_elbow_joint": 0.5})     # dropped
    assert s.arm_offset() is None
    s.onboard_state = "ARMED"
    s._offset_to_current_pose({"left_elbow_joint": 0.9})     # re-captured
    assert s.arm_offset()["v"] == 2
    assert s.arm_offset()["offset"]["left_elbow_joint"] == pytest.approx(0.9)


def test_a_sink_built_without_init_has_no_offset():
    s = sk.RealG1Sink.__new__(sk.RealG1Sink)
    assert s.arm_offset() is None


# ---- sent is what this frame queued, not a stale one --------------------------

class _Q:
    """A sink that may or may not queue a frame."""
    def __init__(self, cmd=None, offset=None, queued=True):
        self.n_retargeted = 0
        self._cmd = cmd
        self._off = offset
        self._queued = queued

    def commanded(self):
        return self._cmd

    def arm_offset(self):
        return self._off

    def queue(self):
        if self._queued:
            self.n_retargeted += 1


def test_sent_for_row_is_the_post_offset_command_when_the_frame_was_queued():
    s = _Q(cmd={"left_elbow_joint": 0.3}, offset={"v": 1, "offset": {}, "captured_at": 1.0})
    before = server.sent_before(s)
    s.queue()
    assert server.sent_for_row(s, before) == {"left_elbow_joint": 0.3}


def test_sent_for_row_is_none_when_the_sink_dropped_the_frame():
    """A dropped frame (missing joints, non-finite) queues nothing; the previous
    command is still in the sink, and writing it as this row's `sent` would
    put a command on a row the robot never received for it."""
    s = _Q(cmd={"left_elbow_joint": 0.3}, queued=False)
    before = server.sent_before(s)
    s.queue()
    assert server.sent_for_row(s, before) is None


def test_sent_for_row_is_none_for_a_sink_without_a_command_view():
    assert server.sent_for_row(server.LogSink(), server.sent_before(server.LogSink())) is None
    assert server.arm_offset_for_row(server.LogSink()) is None


# ---- the recorder writes them -------------------------------------------------

def test_the_row_carries_sent_rounded_and_omits_it_when_absent(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    sid = rec.start("x")["session"]
    rec.note({"t": 1.0, "hands": []}, {"left_elbow_joint": 0.1}, {"state": "ARMED"},
             sent={"left_elbow_joint": 0.123456789})
    rec.note({"t": 1.1, "hands": []}, {"left_elbow_joint": 0.2}, {"state": "ARMED"}, sent=None)
    assert _drain(rec)
    rec.stop()
    rows = _rows(tmp_path / f"{sid}.jsonl")
    assert rows[1]["sent"] == {"left_elbow_joint": 0.12346}
    assert "sent" not in rows[2]


def test_the_offset_is_written_once_per_version_like_the_wrist_zero(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    sid = rec.start("x")["session"]
    off1 = {"v": 1, "offset": {"left_elbow_joint": -0.9412345678}, "captured_at": 1.0}
    off2 = {"v": 2, "offset": {"left_elbow_joint": 0.25}, "captured_at": 2.0}
    rec.note({"t": 1.0, "hands": []}, {}, {"state": "ARMED"}, arm_offset=off1)
    rec.note({"t": 1.1, "hands": []}, {}, {"state": "ARMED"}, arm_offset=off1)
    rec.note({"t": 1.2, "hands": []}, {}, {"state": "ARMED"}, arm_offset=off2)
    rec.note({"t": 1.3, "hands": []}, {}, {"state": "ARMED"}, arm_offset=None)
    assert _drain(rec)
    rec.stop()
    rows = _rows(tmp_path / f"{sid}.jsonl")
    assert rows[1]["arm_offset"] == off1, "full precision, on the first frame"
    assert "arm_offset" not in rows[2], "same version: not repeated"
    assert rows[3]["arm_offset"] == off2
    assert "arm_offset" not in rows[4]


# ---- the validator and replay say whether it is there -------------------------

def _session(tmp_path, with_sent):
    p = tmp_path / "20260916-000000-s.jsonl"
    rows = [{"type": "header", "session": "s", "t_start": 0.0, "v": 1}]
    for i in range(20):
        r = {"t": 0.0 + 0.05 * i, "hands": [], "head": None,
             "robot": {"state": "ARMED", "q": [0.0] * 14, "q_age_s": 0.01, "applied": True},
             "targets": {"left_elbow_joint": 0.5}}
        if with_sent:
            r["sent"] = {"left_elbow_joint": 0.5 - 0.3}
        rows.append(r)
    rows.append({"type": "footer", "frames": 20, "dropped": 0, "t_end": 1.0})
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


def test_the_check_warns_when_armed_rows_have_targets_but_no_sent(tmp_path):
    rep = episode_check.check(_session(tmp_path, with_sent=False))
    codes = [i["code"] for i in rep["issues"]]
    assert "no_sent" in codes
    assert rep["sent_rows"] == 0


def test_the_check_is_quiet_when_sent_is_there(tmp_path):
    rep = episode_check.check(_session(tmp_path, with_sent=True))
    assert "no_sent" not in [i["code"] for i in rep["issues"]]
    assert rep["sent_rows"] == 20

