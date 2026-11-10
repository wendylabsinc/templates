"""stop() must not race the writer thread.

Found while building the GR00T exporter: a session stopped while camera
frames were still queued let the writer thread dereference `_frames_dir`
after stop() had set it to None -- a TypeError nothing caught, which KILLED
the writer thread. From then on every note() queued into a thread that was
no longer there, for the rest of the process. The rows had a milder version
of the same race: a file handle captured before stop() closed it, and a
"write failed, stopping session" path that would close whatever session was
current -- including one started since.

The rules pinned here:
- stop() drains what this session already queued before it writes the footer,
  for a bounded time, then drops and COUNTS the rest. The footer's counts are
  what the file holds.
- The writer never dereferences a closed session, and a late item for a
  session that is gone is a counted no-op, never a traceback, and never a
  reason to touch the session that replaced it.
- note()/note_camera() stay non-blocking, and stop()'s wait is bounded.
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
from session_recorder import SessionRecorder  # noqa: E402


def _frame():
    return {"hands": [{"hand": "right", "landmarks": [0.0] * 75}], "head": {"pos": [0, 1, 0]}}


def _rows(path):
    return [json.loads(l) for l in open(path) if l.strip()]


def _jpg(n=200):
    return b"\xff\xd8" + bytes(n) + b"\xff\xd9"


def test_stop_with_queued_camera_frames_keeps_the_writer_alive(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    sid = rec.start("race")["session"]
    for i in range(60):
        # The pose row FIRST, as it arrives live: rows come at 90 Hz and
        # frames at ~8, and the row is what tells the recorder the robot is
        # ARMED -- which is now what keeps the frame.
        rec.note(_frame(), {"left_elbow_joint": 0.1}, {"state": "ARMED"},
                 cam={"seq": i + 1, "t": 1.0 + i, "age_s": 0.01})
        rec.note_camera({"seq": i + 1, "t": 1.0 + i, "w": 8, "h": 8}, _jpg())
    out = rec.stop()                               # immediately: the queue is full of work
    assert out["ok"]
    rec._thread.join(0.5)
    assert rec._thread.is_alive(), "the writer thread died: an exception escaped _run"
    assert rec.writer_errors == 0
    rows = _rows(rec.path(sid))
    body = [r for r in rows if not r.get("type")]
    footer = rows[-1]
    assert footer["type"] == "footer"
    assert footer["frames"] == len(body) == out["frames"]
    assert footer["frames"] + footer["dropped"] == 60
    frames_dir = os.path.join(str(tmp_path), sid + ".frames")
    jpgs = sorted(n for n in os.listdir(frames_dir) if n.endswith(".jpg")) if os.path.isdir(frames_dir) else []
    index = [l for l in open(os.path.join(frames_dir, "index.jsonl")) if l.strip()] if jpgs else []
    assert footer["cam_frames"] == len(jpgs) == len(index)
    assert footer["cam_frames"] + footer["cam_dropped"] == 60
    rep = episode_check.check(rec.path(sid))
    assert not [i for i in rep["issues"] if i["code"] == "footer_mismatch"], rep["issues"]


def test_everything_queued_is_written_when_the_disk_keeps_up(tmp_path):
    """The normal case: nothing is dropped, the footer says so."""
    rec = SessionRecorder(str(tmp_path))
    sid = rec.start("full")["session"]
    for i in range(30):
        rec.note(_frame(), {"left_elbow_joint": 0.1}, {"state": "ARMED"})
        rec.note_camera({"seq": i + 1, "t": 1.0 + i}, _jpg())
        if i % 10 == 0:
            rec.mark(f"m{i}")
    rec.outcome("success", "place")
    out = rec.stop()
    rows = _rows(rec.path(sid))
    footer = rows[-1]
    assert out["dropped"] == 0 and out["cam_dropped"] == 0
    assert footer["frames"] == 30 and footer["cam_frames"] == 30
    assert footer["marks"] == 3 == len([r for r in rows if r.get("type") == "mark"])
    assert footer["outcomes"]["success"] == 1 == len([r for r in rows if r.get("type") == "outcome"])


def test_a_late_item_after_stop_is_a_counted_no_op(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    sid = rec.start("late")["session"]
    rec.note(_frame(), {"left_elbow_joint": 0.1}, {"state": "ARMED"})
    rec.stop()
    before = open(rec.path(sid)).read()
    rec.note(_frame(), {"left_elbow_joint": 0.2}, {"state": "ARMED"})
    rec.note_camera({"seq": 9}, _jpg())
    assert rec.mark("x")["ok"] is False
    time.sleep(0.05)
    assert rec.status()["late"] == 2
    assert open(rec.path(sid)).read() == before, "a late item must not touch a closed file"
    assert rec._thread.is_alive() and rec.writer_errors == 0


def test_a_stale_item_never_closes_the_session_that_replaced_it(tmp_path):
    """The old failure path: a write for session A fails after A closed, and
    the handler closed 'the current session' -- which was B."""
    rec = SessionRecorder(str(tmp_path))
    a = rec.start("a")["session"]
    rec.note(_frame(), {"left_elbow_joint": 0.1}, {"state": "ARMED"})
    rec.stop()
    b = rec.start("b")["session"]
    # Forge an item that belongs to A into the queue, as the race would leave.
    with rec._lock:
        rec._q.append(("row", a, time.time(), _frame(), {"left_elbow_joint": 0.3},
                       {"state": "ARMED"}, None, None, None, None, None, None))
    rec._wake.set()
    time.sleep(0.1)
    assert rec.recording and rec.status()["recording"] == b
    rec.note(_frame(), {"left_elbow_joint": 0.4}, {"state": "ARMED"})
    out = rec.stop()
    assert out["ok"] and out["frames"] == 1
    rows = _rows(rec.path(b))
    assert rows[-1]["type"] == "footer" and rows[-1]["frames"] == 1
    assert rec.writer_errors == 0


def test_stops_wait_is_bounded_on_a_slow_disk(tmp_path, monkeypatch):
    rec = SessionRecorder(str(tmp_path))
    rec.STOP_DRAIN_S = 0.6
    real = rec._write_camera_locked

    def slow(*a, **k):
        time.sleep(0.25)
        return real(*a, **k)
    monkeypatch.setattr(rec, "_write_camera_locked", slow)
    sid = rec.start("slow")["session"]
    # ARMED opens the camera's gate. This test is about the DRAIN being
    # bounded on a slow disk, so the frames have to reach the queue; without
    # this they are skipped and it measures an empty stop.
    rec.note(_frame(), {"left_elbow_joint": 0.1}, {"state": "ARMED"})
    for i in range(20):
        rec.note_camera({"seq": i + 1, "t": float(i)}, _jpg())
        rec.mark(f"m{i}")
    t0 = time.monotonic()
    out = rec.stop()
    took = time.monotonic() - t0
    assert took < rec.STOP_DRAIN_S + 0.5, f"stop blocked {took:.1f}s"
    rows = _rows(rec.path(sid))
    footer = rows[-1]
    assert footer["type"] == "footer"
    assert footer["cam_frames"] + footer["cam_dropped"] == 20
    assert footer["cam_dropped"] > 0, "the slow disk should have forced counted drops"
    assert footer["marks"] == len([r for r in rows if r.get("type") == "mark"])
    assert out["cam_dropped"] == footer["cam_dropped"]
    assert rec._thread.is_alive() and rec.writer_errors == 0


def test_a_slow_camera_write_never_holds_the_teleop_hot_path_lock(tmp_path,
                                                                  monkeypatch):
    """Camera persistence may be slow, but note() has only a 150 ms budget.

    This is the live failure mode: Quest frames kept arriving while the
    recorder writer held ``_lock`` around a JPEG write.  The frame path needs
    that same lock in note(), so the RealG1Sink target went stale and its
    watchdog repeatedly stopped the stream.
    """
    rec = SessionRecorder(str(tmp_path))
    rec.start("slow-camera")
    rec.note(_frame(), {"left_elbow_joint": 0.1}, {"state": "ARMED"})
    entered = threading.Event()
    release = threading.Event()
    real = rec._write_camera_locked

    def slow(*a, **k):
        entered.set()
        assert release.wait(1.0)
        return real(*a, **k)

    monkeypatch.setattr(rec, "_write_camera_locked", slow)
    rec.note_camera({"seq": 1, "t": 1.0}, _jpg())
    assert entered.wait(1.0)
    timer = threading.Timer(0.25, release.set)
    timer.start()
    t0 = time.perf_counter()
    rec.note(_frame(), {"left_elbow_joint": 0.2}, {"state": "ARMED"})
    blocked = time.perf_counter() - t0
    timer.join()
    rec.stop()
    assert blocked < 0.05, \
        f"camera disk work blocked teleop for {blocked * 1000:.1f} ms"


def test_note_still_does_not_block_while_a_stop_is_draining(tmp_path):
    """The hot path must not inherit stop()'s wait."""
    rec = SessionRecorder(str(tmp_path))
    rec.start("hot")
    t0 = time.perf_counter()
    for _ in range(300):
        rec.note(_frame(), {"left_elbow_joint": 0.1})
    assert (time.perf_counter() - t0) * 1e3 < 200
    rec.stop()
