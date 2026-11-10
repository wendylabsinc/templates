"""Record what the onboard said about each frame, not just its state and q.

The first full-stack episode had the right arm 12-17 deg off on every joint
while the left tracked to 1-2 deg. Working out why took the recorded TARGETS
and the robot's calibration file: 58 % of the right shoulder roll commands
were past the calibrated limit, and nothing in the recording said so. The
onboard knew -- it clamps to the limits and it leashes commands that run ahead
of the arm, and it counts both -- but only in /status, once a second, as a
total. Per frame, per joint, in the episode, is what would have answered it in
one replay instead of an evening.

So: the ack carries which joints the onboard clamped to a limit and which it
leashed; the sink keeps the newest ack per frame; the recorder writes the
whole view; replay reports it. And the sink speaks only when the state or the
message changes, because the idle stream is 20 messages a second.
"""
from __future__ import annotations

import asyncio
import json
import math
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "onboard"))
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))

import g1_onboard as ob  # noqa: E402
from tests.test_onboard_safety import armed_commander  # noqa: E402


# ---- onboard ----------------------------------------------------------------

def _far_target(c, value=1.0, joint=8):
    """A command well past the leash on one joint (8 = right shoulder roll).

    The slew chains from the previous command, so it is seeded AT the target:
    otherwise the first frame after arming steps a few milliradians from the
    measured pose and the leash never sees the gap this test is about.
    """
    import numpy as np
    target = [0.0] * ob.ARM_DIM
    target[joint] = value
    c._last_cmd, c._last_cmd_t = np.asarray(target, float), time.time() - 0.05
    c.last_apply_t = 0.0                 # not rate-limited
    return target


def test_the_leash_records_which_joints_it_held_back():
    c = armed_commander()
    c.measured_arm_q = [0.0] * ob.ARM_DIM
    c.apply(_far_target(c), [0.0] * 7, [0.0] * 7)
    assert c._leash_hit == ["right_shoulder_roll_joint"]
    c._last_cmd, c.last_apply_t = None, 0.0          # re-seed from the measured pose
    c.apply([0.0] * ob.ARM_DIM, [0.0] * 7, [0.0] * 7)
    assert c._leash_hit == []            # cleared on a frame that needed no leash


def test_the_ack_names_the_clamped_and_leashed_joints():
    """Right ELBOW: its calibrated limit (+115 deg) is far enough from the
    measured pose that the clamped command is still past the 10 deg leash,
    so one frame exercises both lists. (The right shoulder roll's +5 deg
    inward limit sits INSIDE the leash, which is its own kind of finding.)"""
    c = armed_commander()
    c.measured_arm_q = [0.0] * ob.ARM_DIM
    arm = ob.sanitize_arm([0.0] * 10 + [3.0] + [0.0] * 3)  # right elbow far past its limit
    clamped_idx = list(ob.LAST_CLAMPED["joints"])           # as ws_handler captures it
    _far_target(c, value=arm[10], joint=10)
    applied = c.apply(arm, [0.0] * 7, [0.0] * 7)
    ack = ob.ack_message(c, 7, applied, clamped_idx)
    assert ack["ack"] == 7 and ack["applied"] is True
    assert ack["clamped"] == ["right_elbow_joint"]
    assert ack["leashed"] == ["right_elbow_joint"]


def test_an_unapplied_ack_reports_no_leash():
    c = armed_commander()
    c.measured_arm_q = [0.0] * ob.ARM_DIM
    c.apply(_far_target(c), [0.0] * 7, [0.0] * 7)
    c.state = "HOLDING"
    c.last_apply_t = 0.0
    applied = c.apply(_far_target(c), [0.0] * 7, [0.0] * 7)
    ack = ob.ack_message(c, 8, applied)
    assert ack["applied"] is False and ack["leashed"] == []


def test_a_hands_only_ack_reports_no_clamp():
    """LAST_CLAMPED belongs to the last ARM sanitised; a fingers-only frame
    must not inherit it."""
    c = armed_commander()
    ob.sanitize_arm([0.0] * 8 + [3.0] + [0.0] * 5)
    ack = ob.ack_message(c, 9, False, None)
    assert ack["clamped"] == []


# ---- sink -------------------------------------------------------------------

def _sink():
    import sink_realg1 as sk
    s = sk.RealG1Sink.__new__(sk.RealG1Sink)
    s._latest, s._latest_t = None, 0.0
    s._lock = threading.Lock()
    s.url = "ws://robot.test/ws"
    s.n_sent = s.n_retargeted = 0
    s.onboard_state, s.onboard_msg, s.onboard_q, s.onboard_q_t = "?", "", None, 0.0
    s.n_acked = s.n_applied_ack = 0
    s.last_ack = s.last_applied = None
    s.connected = True
    return s


class _Msg:
    def __init__(self, d):
        import aiohttp
        self.type = aiohttp.WSMsgType.TEXT
        self.data = json.dumps(d)


class _Stream:
    def __init__(self, msgs):
        self._it = iter(msgs)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return _Msg(next(self._it))
        except StopIteration:
            raise StopAsyncIteration


def test_robot_view_carries_the_newest_ack_per_frame():
    s = _sink()
    asyncio.run(s._read(_Stream([
        {"ack": 41, "state": "ARMED", "applied": True, "q": [0.3] * 14,
         "clamped": ["right_shoulder_roll_joint"],
         "leashed": ["right_shoulder_roll_joint", "right_elbow_joint"],
         "waist": {"cmd": 0.2, "q": 0.19, "clamped": False, "leashed": False}},
    ])))
    v = s.robot_view()
    assert v["state"] == "ARMED" and v["q"] == pytest.approx([0.3] * 14)
    assert v["ack"] == 41 and v["applied"] is True
    assert v["clamped"] == ["right_shoulder_roll_joint"]
    assert v["leashed"] == ["right_shoulder_roll_joint", "right_elbow_joint"]
    assert v["q_age_s"] is not None and v["q_age_s"] < 1.0
    assert v["waist"]["cmd"] == 0.2
    assert v["waist"]["age_s"] < 1.0


def test_robot_view_is_honest_before_any_ack():
    v = _sink().robot_view()
    assert v["q"] is None and v["q_age_s"] is None and v["ack"] is None
    assert v["applied"] is None and v["clamped"] == [] and v["leashed"] == []


def test_an_ack_without_clamp_fields_from_an_older_onboard_reads_as_empty():
    s = _sink()
    asyncio.run(s._read(_Stream([{"ack": 1, "state": "ARMED", "applied": True, "q": [0.0] * 14}])))
    v = s.robot_view()
    assert v["clamped"] == [] and v["leashed"] == [] and v["applied"] is True


def test_an_unapplied_ack_does_not_refresh_the_last_applied_waist_command():
    s = _sink()
    waist = {"cmd": 0.2, "q": 0.19, "clamped": False, "leashed": False}
    asyncio.run(s._read(_Stream([{"ack": 1, "state": "ARMED", "applied": True,
                                  "waist": waist}])))
    applied_t = s.last_waist_t
    asyncio.run(s._read(_Stream([{"ack": 2, "state": "ARMED", "applied": False,
                                  "waist": waist, "refused": "rate_limit"}])))
    assert s.last_waist_t == applied_t
    assert s.last_waist == waist


def test_the_sink_speaks_only_when_the_state_or_message_changes(capsys):
    """The idle stream is 20 messages a second with no text. The old rule
    ("state changed OR a message has ever been received") printed one line
    per idle message once any reply had carried text."""
    s = _sink()
    asyncio.run(s._read(_Stream(
        [{"state": "HOLDING", "msg": "HOLDING — arms grabbed"}]
        + [{"state": "HOLDING", "q": [0.0] * 14}] * 50
        + [{"state": "ARMED", "q": [0.0] * 14}]
        + [{"state": "ARMED", "msg": "refused: something"}])))
    lines = [l for l in capsys.readouterr().out.splitlines() if "onboard:" in l]
    assert len(lines) == 3, lines


# ---- recorder ---------------------------------------------------------------

def test_the_recorder_writes_the_whole_robot_view_per_frame(tmp_path):
    from session_recorder import SessionRecorder
    rec = SessionRecorder(str(tmp_path))
    rec.start("x")
    rec.note({"t": 1.0, "hands": []}, {"left_elbow_joint": 0.1},
             {"state": "ARMED", "q": [0.0] * 14, "q_age_s": 0.012, "ack": 5,
              "applied": True, "clamped": ["right_shoulder_roll_joint"], "leashed": [], "sent": 6})
    time.sleep(0.3)
    sid = rec.stop()["session"]
    rows = [json.loads(l) for l in open(tmp_path / f"{sid}.jsonl")]
    body = [r for r in rows if r.get("type") not in ("header", "footer")]
    assert body[0]["robot"]["clamped"] == ["right_shoulder_roll_joint"]
    assert body[0]["robot"]["ack"] == 5 and body[0]["robot"]["applied"] is True


# ---- replay -----------------------------------------------------------------
