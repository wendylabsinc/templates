"""The sink's side of the per-frame ack, and the absolute (jog) frame path."""
import asyncio
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))

import sink_realg1 as sr  # noqa: E402
from retarget_g1 import ARM_LEFT, ARM_RIGHT, DEX3_LEFT, DEX3_RIGHT  # noqa: E402


def bare_sink():
    """A sink with no threads and no socket."""
    s = sr.RealG1Sink.__new__(sr.RealG1Sink)
    s.url = "ws://10.0.0.1:8090/ctrl?key=secret"
    s._latest = None
    s._latest_t = 0.0
    s._lock = __import__("threading").Lock()
    s._cmd_q = []
    s._cmd_lock = __import__("threading").Lock()
    s.n_sent = s.n_retargeted = 0
    s.onboard_state = "HOLDING"
    s.onboard_msg = ""
    s.onboard_q = None
    s.onboard_q_t = 0.0
    s.onboard_gains = None
    s._arm_offset = None
    s._warned_no_q = False
    s.seq = 0
    s.waist_seq = 0
    s.n_acked = s.n_applied_ack = 0
    s.last_ack = None
    s._last_hands = None
    s.connected = False
    return s


class Msg:
    type = __import__("aiohttp").WSMsgType.TEXT

    def __init__(self, d):
        self.data = json.dumps(d)


class FakeWs:
    def __init__(self, msgs):
        self._msgs = list(msgs)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._msgs:
            raise StopAsyncIteration
        return Msg(self._msgs.pop(0))


def test_acks_update_measured_and_counters():
    s = bare_sink()
    asyncio.run(s._read(FakeWs([
        {"ack": 5, "state": "ARMED", "applied": True, "q": [0.1] * 14},
        {"ack": 6, "state": "ARMED", "applied": False, "q": [0.2] * 14},
        {"state": "ARMED", "q": [0.3] * 14},                  # idle stream
        {"state": "ARMED", "msg": "", "q": [0.4] * 14, "gains": {"left_elbow_joint": {"kp": 80, "kd": 3}}},
    ])))
    assert s.n_acked == 2 and s.n_applied_ack == 1 and s.last_ack == 6
    q, age = s.measured()
    assert q == [0.4] * 14 and age is not None and age < 1.0
    assert s.onboard_gains["left_elbow_joint"]["kp"] == 80
    st = s.status()
    assert st["acked"] == 2 and st["applied"] == 1 and st["q_age_s"] is not None


def test_batched_superseded_receipt_counts_without_overwriting_measurement():
    s = bare_sink()
    s.onboard_q = [0.4] * 14
    asyncio.run(s._read(FakeWs([
        {"superseded_acks": [5, 6, 7], "state": "ARMED"},
    ])))
    assert s.n_acked == 3 and s.n_applied_ack == 0
    assert s.last_ack == 7 and s.last_refused == "superseded"
    assert s.onboard_q == [0.4] * 14


def test_independent_hand_receipts_do_not_masquerade_as_arm_acks():
    s = bare_sink()
    asyncio.run(s._read(FakeWs([
        {"hand_ack": 10, "state": "ARMED", "applied": True,
         "refused": None, "superseded": [8, 9]},
        {"hand_ack": 11, "state": "HOLDING", "applied": False,
         "refused": "not_armed", "superseded": []},
    ])))
    assert s.n_acked == 0 and s.n_applied_ack == 0
    assert s.n_hand_acked == 2 and s.n_hand_applied_ack == 1
    delivery = s.status()["hand_delivery"]
    assert delivery == {"acked": 2, "applied": 1, "last_ack": 11,
                        "last_applied": False, "last_refused": "not_armed",
                        "last_superseded": []}
    view = s.robot_view()
    assert view["hand_ack"] == 11 and view["hand_applied"] is False
    assert view["hand_refused"] == "not_armed"


def test_independent_waist_receipts_do_not_masquerade_as_arm_acks():
    s = bare_sink()
    asyncio.run(s._read(FakeWs([
        {"waist_ack": 20, "state": "ARMED", "applied": True,
         "refused": None, "superseded": [18, 19],
         "waist": {"cmd": 0.2, "q": 0.18, "clamped": False, "leashed": False}},
        {"ack": 21, "state": "ARMED", "applied": True,
         "waist_applied": False,
         "waist": {"cmd": 9.0, "q": 9.0, "clamped": False, "leashed": False}},
    ])))
    assert s.n_acked == 1 and s.n_waist_acked == 1
    assert s.last_waist["cmd"] == pytest.approx(0.2)
    delivery = s.status()["waist_delivery"]
    assert delivery["applied"] == 1 and delivery["last_ack"] == 20
    assert delivery["last_superseded"] == [18, 19]


def test_stamp_adds_seq_and_time():
    s = bare_sink()
    f1 = s._stamp({"arm": [0.0] * 14})
    f2 = s._stamp({"arm": [0.0] * 14})
    assert f1["seq"] == 1 and f2["seq"] == 2
    assert abs(f2["t_sent"] - time.time()) < 1.0
    assert "seq" not in {"arm": [0.0] * 14}       # the original is untouched


def test_absolute_frames_accept_any_arm_subset_and_skip_the_offset():
    s = bare_sink()
    s.onboard_state = "ARMED"
    s.onboard_q = [0.0] * 14
    hands = {n: 0.0 for n in DEX3_LEFT + DEX3_RIGHT}
    # one joint only: refused on the retargeter path...
    asyncio.run(s.apply(dict(hands, left_elbow_joint=0.9)))
    assert s._latest is None
    # ...accepted on the absolute path, and NOT offset to the measured pose
    asyncio.run(s.apply(dict(hands, left_elbow_joint=0.9), absolute=True))
    assert s._latest["arm_partial"] == {"left_elbow_joint": 0.9}
    assert "arm" not in s._latest
    assert s.last_hands() == ([0.0] * 7, [0.0] * 7)


def test_absolute_frames_still_need_finite_numbers():
    s = bare_sink()
    hands = {n: 0.0 for n in DEX3_LEFT + DEX3_RIGHT}
    asyncio.run(s.apply(dict(hands, left_elbow_joint=float("nan")), absolute=True))
    assert s._latest is None


def test_payload_commands_are_whitelisted():
    s = bare_sink()
    assert s.command_payload({"cmd": "gains", "joints": {"left_elbow_joint": {"kp": 50}}})
    assert s.command_payload({"cmd": "gains_save"})
    assert not s.command_payload({"cmd": "arm"})            # names go through command()
    assert not s.command_payload({"cmd": "relax", "go_home": True})
    assert not s.command_payload("gains")
    assert [c["cmd"] for c in s._cmd_q] == ["gains", "gains_save"]


def test_arm_seeds_the_hand_hold_from_measured_pose():
    s = bare_sink()
    s.connected = True
    s.last_hand_q = {"left": [0.31] * 7, "right": [0.42] * 7}
    assert s.command("arm")
    assert s.last_hands() == ([0.31] * 7, [0.42] * 7)


def test_onboard_http_url_drops_the_secret():
    s = bare_sink()
    assert s.onboard_http_url() == "http://10.0.0.1:8090/status"
    s.url = "wss://robot.example:8090/ctrl?key=x"
    assert s.onboard_http_url("/status") == "https://robot.example:8090/status"
