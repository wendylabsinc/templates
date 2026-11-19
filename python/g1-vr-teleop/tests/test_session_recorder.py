"""The session recorder: it must never be able to stall teleop.

That is the property under test more than any other. A recorder that blocks
the frame path degrades the exact thing it exists to measure, and it would do
so invisibly -- as a frame rate that quietly drops whenever the disk is busy.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))
from session_recorder import SessionRecorder  # noqa: E402


def _frame(n=2):
    return {"hands": [{"hand": s, "landmarks": [0.1] * 75,
                       "wrist": [0.1, 0.2, 0.3], "quat": [0, 0, 0, 1],
                       "fabricated": 0}
                      for s in ("left", "right")[:n]],
            "head": {"pos": [0, 1.6, 0], "quat": [0, 0, 0, 1]}}


def _drain(r, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not r.status()["queued"]:
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def rec(tmp_path):
    r = SessionRecorder(str(tmp_path / "sessions"))
    yield r
    r.close()


def _rows(path):
    with open(path) as fh:
        return [json.loads(l) for l in fh if l.strip()]


def test_a_session_round_trips_to_disk(rec):
    sid = rec.start("probe")["session"]
    for _ in range(5):
        rec.note(_frame(), {"left_elbow_joint": 0.5})
    assert _drain(rec)
    out = rec.stop()
    assert out["frames"] == 5 and out["dropped"] == 0

    rows = _rows(rec.path(sid))
    assert rows[0]["type"] == "header" and rows[0]["session"] == sid
    assert rows[-1]["type"] == "footer" and rows[-1]["frames"] == 5
    body = rows[1:-1]
    assert len(body) == 5
    assert body[0]["targets"]["left_elbow_joint"] == pytest.approx(0.5)
    assert len(body[0]["hands"]) == 2
    assert body[0]["head"] is not None


def test_note_does_not_block_the_caller(rec):
    """The frame path is an asyncio handler at 90 fps."""
    rec.start()
    t0 = time.perf_counter()
    for _ in range(500):
        rec.note(_frame(), {"left_elbow_joint": 0.1})
    dt = (time.perf_counter() - t0) * 1e3
    assert dt < 200, f"500 notes took {dt:.0f} ms; the frame path is waiting on disk"
    rec.stop()


def test_a_backed_up_queue_drops_rather_than_blocks(rec):
    """A recorder that can stall teleop is a hazard; one with a gap is a recorder."""
    rec.start()
    for _ in range(rec.QUEUE_MAX + 500):
        rec.note(_frame())
    st = rec.status()
    assert st["queued"] <= rec.QUEUE_MAX
    out = rec.stop()
    assert out["dropped"] > 0, "nothing was dropped; did it block instead?"


def test_drops_are_reported_in_the_footer(rec):
    """A silent gap looks like a frame-rate change at analysis time."""
    sid = rec.start()["session"]
    for _ in range(rec.QUEUE_MAX + 200):
        rec.note(_frame())
    rec.stop()
    footer = _rows(rec.path(sid))[-1]
    assert footer["type"] == "footer" and footer["dropped"] > 0


def test_notes_outside_a_session_are_ignored(rec):
    rec.note(_frame())                     # never started
    assert rec.status()["recording"] is None
    assert rec.sessions() == []


def test_starting_twice_closes_the_first_session(rec):
    a = rec.start("one")["session"]
    rec.note(_frame())
    assert _drain(rec)
    b = rec.start("two")["session"]
    rec.note(_frame())
    assert _drain(rec)
    rec.stop()
    assert a != b
    # the first file must still be well-formed, not truncated mid-stream
    rows = _rows(rec.path(a))
    assert rows[-1]["type"] == "footer"
    assert {s["session"] for s in rec.sessions()} == {a, b}


def test_stopping_when_idle_is_refused_not_crashed(rec):
    assert rec.stop()["ok"] is False


def test_delayed_stop_cannot_close_a_replacement_session(rec):
    old = rec.start("old")["session"]
    rec.stop(expected_sid=old)
    new = rec.start("new")["session"]
    refused = rec.stop(expected_sid=old)
    assert not refused["ok"]
    assert refused["error"] == "session ownership changed"
    assert rec.status()["recording"] == new
    assert rec.stop(expected_sid=new)["ok"]


def test_a_session_name_cannot_escape_the_directory(rec):
    sid = rec.start("../../etc/passwd")["session"]
    assert "/" not in sid and ".." not in sid
    assert os.path.dirname(rec.path(sid)) == rec.dir
    rec.stop()


def test_path_refuses_traversal(rec):
    rec.start("x")
    rec.stop()
    for bad in ("../secrets", "a/b", "..", ".hidden", ""):
        assert rec.path(bad) is None, f"{bad!r} resolved to a file"


def test_targets_are_optional(rec):
    """Recording input alone is still worth doing when nothing solved yet."""
    sid = rec.start()["session"]
    rec.note(_frame())
    assert _drain(rec)
    rec.stop()
    body = _rows(rec.path(sid))[1:-1]
    assert "targets" not in body[0]


def test_non_numeric_targets_are_skipped_not_fatal(rec):
    sid = rec.start()["session"]
    rec.note(_frame(), {"left_elbow_joint": 0.25, "note": "not a number"})
    assert _drain(rec)
    rec.stop()
    body = _rows(rec.path(sid))[1:-1]
    assert body[0]["targets"] == {"left_elbow_joint": 0.25}


def test_one_handed_frames_record_fine(rec):
    sid = rec.start()["session"]
    rec.note(_frame(n=1))
    assert _drain(rec)
    rec.stop()
    assert len(_rows(rec.path(sid))[1]["hands"]) == 1


def test_the_recording_is_replayable_as_the_tools_expect(rec):
    """Offline tools read `hands` lists with flat 75-float landmarks."""
    sid = rec.start()["session"]
    rec.note(_frame())
    assert _drain(rec)
    rec.stop()
    row = _rows(rec.path(sid))[1]
    for h in row["hands"]:
        assert h["hand"] in ("left", "right")
        assert len(h["landmarks"]) == 75
        assert len(h["quat"]) == 4 and len(h["wrist"]) == 3


def test_a_recorder_with_nowhere_to_write_is_inert():
    """os.listdir(None) lists the CURRENT directory instead of failing.

    Without an explicit guard, a recorder that found no writable location would
    report whatever files happen to sit beside the process as if they were
    sessions.
    """
    r = SessionRecorder(None)
    try:
        assert r.start()["ok"] is False
        assert r.sessions() == []
        assert r.path("anything") is None
        r.note(_frame())                   # must not raise
        assert r.status()["recording"] is None
    finally:
        r.close()


def test_pick_dir_falls_back_to_a_writable_place(tmp_path):
    got = SessionRecorder.pick_dir(str(tmp_path / "chosen"))
    assert got == str(tmp_path / "chosen")
    assert os.path.isdir(got)


def test_pick_dir_skips_an_unwritable_candidate(tmp_path, monkeypatch):
    """A hardcoded persist path that is not mounted must not swallow sessions."""
    bad = tmp_path / "nope"
    bad.mkdir()
    bad.chmod(0o500)                       # readable, not writable
    try:
        got = SessionRecorder.pick_dir(str(bad))
        assert got != str(bad), "an unwritable directory was accepted"
        assert got is None or os.access(got, os.W_OK)
    finally:
        bad.chmod(0o700)


def test_the_robot_state_is_recorded_per_frame(rec):
    """A frozen stretch must be distinguishable from a still demonstration.

    Without this the two look identical in the data, and a frozen stretch as
    training data teaches "the operator's hands move and the arm does not".
    """
    sid = rec.start()["session"]
    rec.note(_frame(), {"left_elbow_joint": 0.1}, {"state": "ARMED"})
    rec.note(_frame(), {"left_elbow_joint": 0.2}, {"state": "HOLDING",
                                                   "msg": "liveness"})
    assert _drain(rec)
    rec.stop()
    body = _rows(rec.path(sid))[1:-1]
    assert body[0]["robot"]["state"] == "ARMED"
    assert body[1]["robot"]["state"] == "HOLDING"
    assert body[1]["robot"]["msg"] == "liveness"


def test_frames_without_robot_state_still_record(rec):
    """Sessions from a bridge that does not report state are still valid."""
    sid = rec.start()["session"]
    rec.note(_frame(), {"left_elbow_joint": 0.1})
    assert _drain(rec)
    rec.stop()
    assert "robot" not in _rows(rec.path(sid))[1]


def test_measured_joints_are_recorded_beside_the_command(rec):
    """An episode of commands alone is half a demonstration.

    During a freeze, or any stretch the arm cannot keep up with, commanded and
    measured diverge completely -- and the commanded half is the one that keeps
    moving. Training on it teaches the arm to do what it was told rather than
    what it did.
    """
    sid = rec.start()["session"]
    rec.note(_frame(), {"left_elbow_joint": 0.9},
             {"state": "ARMED", "q": [0.1] * 14})
    assert _drain(rec)
    rec.stop()
    row = _rows(rec.path(sid))[1]
    assert row["targets"]["left_elbow_joint"] == pytest.approx(0.9)
    assert len(row["robot"]["q"]) == 14
    assert row["robot"]["q"][0] == pytest.approx(0.1)
