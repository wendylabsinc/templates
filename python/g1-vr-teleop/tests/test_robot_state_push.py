"""The bridge tells the headset where the robot IS, thirty times a second.

The VR page draws a miniature of the robot: measured pose solid, the command
the sink actually queued as a ghost. Until now nothing flowed from the bridge
to the headset except errors; the legend polled /control/status at 2 Hz,
which is a legend's rate and not an instrument's. A moving arm drawn from a
2 Hz sample is a stuttering arm, and a stuttering arm reads as a robot in
trouble.

So the state rides the /teleop socket the headset already holds, pushed by its
own task rather than piggybacked on incoming frames: when the headset loses
tracking it stops SENDING frames, and that is exactly when the operator most
wants to see where the arms are.
"""
import asyncio
import json
import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))
import server                                                    # noqa: E402
from retarget_g1 import ARM_LEFT, ARM_RIGHT                      # noqa: E402

_ARM = ARM_LEFT + ARM_RIGHT


class _Sink:
    """What RealG1Sink exposes, and nothing else."""
    def __init__(self):
        self.onboard_state = "ARMED"
        self.onboard_q = [0.1 * i for i in range(14)]
        self.onboard_q_t = time.monotonic()
        self.onboard_body = {"idx": [12, 15, 16], "q": [0.4, 0.1, 0.2],
                             "dq": [0, 0, 0], "tau": [0, 0, 0]}
        self.last_hand_q = {"left": [0.5] * 7, "right": [0.6] * 7}
        self._cmd = dict(zip(_ARM, [1.0 + 0.1 * i for i in range(14)]))
        self._cmd["waist_yaw_joint"] = 0.45
        self._hands = ([0.7] * 7, [0.8] * 7)

    def commanded(self):
        return dict(self._cmd)

    def last_hands(self):
        return self._hands


class _Bare:
    """A LogSink or SimSink: no onboard, no measured anything."""


def test_the_message_carries_measured_and_commanded_side_by_side():
    m = server.robot_state_message(_Sink())
    assert m["type"] == "robot"
    assert m["state"] == "ARMED"
    assert m["q"] == pytest.approx([0.1 * i for i in range(14)])
    assert m["q_age_s"] < 0.1
    assert m["waist"] == pytest.approx(0.4), "motor 12 out of the body vector"
    assert m["hand_q"] == {"left": [0.5] * 7, "right": [0.6] * 7}
    assert m["cmd"]["arm"] == pytest.approx([1.0 + 0.1 * i for i in range(14)])
    assert m["cmd"]["waist"] == pytest.approx(0.45)
    assert m["cmd"]["hands"] == {"left": [0.7] * 7, "right": [0.8] * 7}


def test_the_command_is_what_the_sink_queued_not_the_operators_targets():
    """commanded() has the arming offset subtracted; the retargeter's targets
    do not. Drawn against the measured arm, the raw targets would show a
    permanent 54-degree gap on an elbow nothing is touching -- the same
    mistake the contact monitor made once."""
    src = server.robot_state_message.__code__.co_names
    assert "commanded" in src
    body = (ROOT / "teleop" / "bridge" / "server.py").read_text().split("def robot_state_message")[1].split("\ndef ")[0]
    code = body.split('"""')[2]          # after the docstring
    assert "retargeter" not in code and "arm_now" not in code


def test_a_partial_command_keeps_the_wire_order_with_gaps():
    """Forearm-only modes command four joints a side. The page indexes the
    list by arm_order, so the absent joints are null, not dropped."""
    s = _Sink()
    s._cmd = {"left_elbow_joint": 1.5, "right_wrist_roll_joint": -0.2}
    arm = server.robot_state_message(s)["cmd"]["arm"]
    assert len(arm) == 14
    assert arm[_ARM.index("left_elbow_joint")] == 1.5
    assert arm[_ARM.index("right_wrist_roll_joint")] == -0.2
    assert arm.count(None) == 12


def test_a_sink_with_nothing_to_say_says_so_without_crashing():
    m = server.robot_state_message(_Bare())
    assert m["type"] == "robot"
    assert m["q"] is None and m["q_age_s"] is None
    assert m["waist"] is None and m["hand_q"] is None
    assert m["cmd"] == {"arm": None, "waist": None, "hands": None}


def test_the_age_is_reported_so_the_page_can_refuse_a_stale_arm():
    """A measurement from a robot that stopped replying must not be drawn as
    the robot's current pose. The bridge does not hide it; it stamps it."""
    s = _Sink()
    s.onboard_q_t = time.monotonic() - 3.0
    assert server.robot_state_message(s)["q_age_s"] >= 3.0


def test_a_body_without_motor_12_gives_no_waist():
    s = _Sink()
    s.onboard_body = {"idx": [15, 16], "q": [0.1, 0.2], "dq": [0, 0]}
    assert server.robot_state_message(s)["waist"] is None


def test_the_message_is_small():
    """Thirty a second, over the same socket as the hand frames. dq, tau,
    temperatures and the onboard's prose do not belong in it."""
    m = server.robot_state_message(_Sink())
    assert len(json.dumps(m)) < 900
    for k in ("body", "hand_state", "msg", "dq", "tau"):
        assert k not in m


class _WS:
    def __init__(self, close_after=None):
        self.sent = []
        self.closed = False
        self._close_after = close_after

    async def send_json(self, m):
        self.sent.append(m)
        if self._close_after is not None and len(self.sent) >= self._close_after:
            self.closed = True


def test_the_push_is_paced_to_its_rate_and_stops_with_the_socket():
    ws = _WS()

    async def run():
        task = asyncio.create_task(server.push_robot_state(ws, _Sink(), hz=30))
        await asyncio.sleep(0.3)
        ws.closed = True
        await asyncio.wait_for(task, 1.0)
    asyncio.run(run())
    # 0.3 s at 30 Hz is 9; allow the loop some slack either way but not
    # a runaway and not a trickle.
    assert 5 <= len(ws.sent) <= 11, len(ws.sent)
    assert all(m["type"] == "robot" for m in ws.sent)


def test_a_send_that_fails_ends_the_push_rather_than_the_handler():
    class _Broken(_WS):
        async def send_json(self, m):
            raise ConnectionResetError("gone")

    async def run():
        await asyncio.wait_for(server.push_robot_state(_Broken(), _Sink(), hz=30), 1.0)
    asyncio.run(run())            # returns, does not raise


def test_the_handler_starts_the_push_and_cancels_it_on_exit():
    body = (ROOT / "teleop" / "bridge" / "server.py").read_text().split("async def ws_handler")[1].split("\nasync def ")[0]
    assert "push_robot_state(" in body
    assert "push_task.cancel()" in body, "a push that outlives its socket sends into the void forever"
