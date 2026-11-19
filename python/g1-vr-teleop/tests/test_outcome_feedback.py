"""Pressing A must say whether it landed.

2026-09-16: the operator recorded a full session, pressed A, and the file has
no label. `outcome()` refuses when nothing is recording -- correctly, the file
is closed and sealed -- and returned that refusal into a void. Nothing in the
headset said anything either way, so a press that worked and a press that
vanished looked identical.

It is the one control whose entire purpose is labelling the data, and it was
the only one with no feedback at all.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_p = str(Path(__file__).resolve().parent.parent / "teleop" / "bridge")
if _p not in sys.path:
    sys.path.insert(0, _p)

from session_recorder import SessionRecorder  # noqa: E402
import server as srv  # noqa: E402


@pytest.fixture
def rec(tmp_path):
    r = SessionRecorder(str(tmp_path))
    yield r
    r.close()


def test_a_verdict_while_recording_is_kept(rec):
    rec.start("s")
    out = rec.outcome("success", "", "button:right")
    assert out["ok"] is True and out["outcomes"]["success"] == 1


def test_a_verdict_just_after_a_stop_names_the_session_it_missed(rec):
    """The refusal must be legible: the operator pressed a real button."""
    sid = rec.start("s")["session"]
    rec.stop()
    out = rec.outcome("success")
    assert out["ok"] is False
    assert out["late"] is True, "a press seconds after the stop is not 'not recording'"
    assert out["session"] == sid, "it must name the session the verdict was for"
    assert "BEFORE stopping" in out["error"]
    assert "control page" in out["error"], "and say where it can still be added"


def test_a_verdict_long_after_any_session_is_a_plain_refusal(rec, monkeypatch):
    sid = rec.start("s")["session"]
    rec.stop()
    monkeypatch.setattr(rec, "_last_stop_t",
                        time.monotonic() - rec.OUTCOME_GRACE_S - 1)
    out = rec.outcome("success")
    assert out["ok"] is False and not out.get("late")


def test_a_verdict_before_any_session_is_a_plain_refusal(rec):
    out = rec.outcome("success")
    assert out["ok"] is False and not out.get("late")


def test_the_grace_is_long_enough_to_cover_the_reach():
    """The press follows letting go of the can, which follows the stop."""
    assert SessionRecorder.OUTCOME_GRACE_S >= 3.0


def test_face_buttons_do_not_label_a_recording(rec):
    rec.start("s")
    frame = {"hands": [{"hand": "right", "source": "controller",
                        "buttons": {"primary": True}}]}
    srv.headset_buttons(frame, {}, {"source": "xr", "session": rec})
    assert rec.stop()["outcomes"] == {"success": 0, "fail": 0, "abort": 0}


def test_the_page_has_no_manual_label_feedback():
    js = (Path(__file__).resolve().parent.parent / "teleop" / "web" / "teleop.js").read_text()
    assert "outcome: d.outcome" not in js
    assert "mark an attempt" not in js
