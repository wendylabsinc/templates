"""The camera records the demonstration, not the room.

A session left open records the camera for as long as it is open. One on
2026-09-16 sat open 18.5 minutes for 51 seconds of teleop and produced 9360
frames, 278 MB -- of which 418 were referenced by any pose row and 216 by an
ARMED one. The rest is a camera filming an empty table, and it was sealed,
pulled over the network and committed with the rest.

ARMED is the gate because ARMED is what "demonstration" means everywhere else
here: the validator's share, the exporter's rows, the attempt boundaries. A
frame outside it is not training data under any of those definitions.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_p = str(Path(__file__).resolve().parent.parent / "teleop" / "bridge")
if _p not in sys.path:
    sys.path.insert(0, _p)

from session_recorder import SessionRecorder  # noqa: E402

ARMED = {"state": "ARMED — following"}
HOLDING = {"state": "HOLDING — arms frozen"}


@pytest.fixture
def rec(tmp_path):
    r = SessionRecorder(str(tmp_path))
    r.start("t")
    yield r
    r.close()


def _cam(r, n=1):
    for i in range(n):
        r.note_camera({"seq": i, "t": 0.0}, b"\xff\xd8jpeg")


def _queued_cams(r):
    return sum(1 for it in r._q if it[0] == "cam")


def test_no_camera_frame_is_kept_before_the_robot_is_ever_armed(rec):
    _cam(rec, 5)
    assert _queued_cams(rec) == 0
    assert rec.n_cam_skipped == 5


def test_frames_are_kept_once_a_row_reports_armed(rec):
    rec.note({"hands": []}, {}, ARMED)
    _cam(rec, 3)
    assert _queued_cams(rec) == 3
    assert rec.n_cam_skipped == 0


def test_the_tail_keeps_the_release_then_stops(rec, monkeypatch):
    """The hand opens after the arm is told to hold; that is the half a
    trainer needs to see whether the grasp worked."""
    import session_recorder as sr
    t = [100.0]
    monkeypatch.setattr(sr.time, "monotonic", lambda: t[0])
    rec.note({"hands": []}, {}, ARMED)
    t[0] += rec.CAM_TAIL_S - 0.5           # inside the tail
    _cam(rec, 2)
    assert _queued_cams(rec) == 2
    t[0] += 1.0                             # past it
    _cam(rec, 2)
    assert _queued_cams(rec) == 2, "kept filming after the tail ran out"
    assert rec.n_cam_skipped == 2


def test_skipped_is_counted_apart_from_dropped(rec):
    """Dropped means the writer fell behind and the episode has a hole.
    Skipped means the recorder declined a picture of an empty table."""
    _cam(rec, 4)
    assert rec.n_cam_skipped == 4 and rec.n_cam_dropped == 0
    st = rec.status()
    assert st["cam_skipped_not_armed"] == 4
    assert st["cam_dropped"] == 0


def test_the_stop_reply_says_how_many_were_skipped(rec):
    rec.note({"hands": []}, {}, ARMED)
    _cam(rec, 1)
    out = rec.stop()
    assert out["cam_skipped_not_armed"] == 0
    r2 = SessionRecorder(str(Path(rec.dir)))
    r2.start("t2")
    _cam(r2, 6)
    out2 = r2.stop()
    assert out2["cam_skipped_not_armed"] == 6
    assert out2["cam_frames"] == 0
    r2.close()


def test_the_counters_reset_between_sessions(rec):
    _cam(rec, 3)
    assert rec.n_cam_skipped == 3
    rec.start("again")
    assert rec.n_cam_skipped == 0 and rec._armed_t is None
    _cam(rec, 1)
    assert rec.n_cam_skipped == 1, "the previous session's ARMED leaked in"


def test_a_row_without_a_robot_state_does_not_open_the_gate(rec):
    for robot in (None, {}, {"state": None}, HOLDING):
        rec.note({"hands": []}, {}, robot)
    _cam(rec, 2)
    assert _queued_cams(rec) == 0


def test_the_tail_is_long_enough_to_be_worth_having():
    assert SessionRecorder.CAM_TAIL_S >= 1.0
