"""Home must close the hands the way the grip closes them -- one owner.

The operator: "they dont go full close at home". go_home commanded
g1_dex3.CLOSED_*, a hand-copied grip_q(side, 1.0) taken when GRIP_THUMB0 was
0, so the thumb stayed beside the fingers however the live knob was set. The
bridge owns the grip pose; the onboard held a stale copy. Now the bridge sends
the pose it would command at a full squeeze with the home command, and the
onboard uses it -- falling back to its own copy only when nothing valid came.
"""
import asyncio
import os
import sys
import time
import types

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "teleop", "bridge"))
sys.path.insert(0, os.path.join(ROOT, "onboard"))

import retarget_g1 as rg  # noqa: E402
import server  # noqa: E402
import sink_realg1 as sr  # noqa: E402
import g1_onboard as ob  # noqa: E402
import dex3_stall as ds  # noqa: E402
from tests.test_endpoint_gating import _req, KEY  # noqa: E402
from tests.test_onboard_safety import FakeArm, FakeHand  # noqa: E402
from tests.test_sink_ack import bare_sink  # noqa: E402


@pytest.fixture
def thumb0():
    old = rg.GRIP_THUMB0
    yield
    rg.set_grip_thumb0(old)


# ---- the bridge builds the payload from the live grip ------------------------

def test_home_payload_is_grip_q_at_a_full_squeeze(thumb0):
    rg.set_grip_thumb0(1.0)
    p = server.home_payload()
    assert set(p) == {"hands"} and set(p["hands"]) == {"left", "right"}
    assert p["hands"]["left"] == pytest.approx(rg.grip_q("left", 1.0))
    assert p["hands"]["right"] == pytest.approx(rg.grip_q("right", 1.0))
    assert p["hands"]["right"][0] != 0.0, "thumb_0 follows the knob"
    rg.set_grip_thumb0(0.0)
    assert server.home_payload()["hands"]["right"][0] == 0.0


def test_the_sink_carries_a_payload_with_the_command():
    s = bare_sink()
    assert s.command("home", payload={"hands": {"left": [0.0] * 7, "right": [0.0] * 7}})
    assert s.command("hold")
    assert s._cmd_q[0] == {"cmd": "home", "hands": {"left": [0.0] * 7, "right": [0.0] * 7}}
    assert s._cmd_q[1] == "hold", "commands without a payload are unchanged"
    assert s.command("bogus", payload={"x": 1}) is False


def test_send_home_holds_then_homes_with_the_hands(thumb0):
    rg.set_grip_thumb0(-1.0)
    s = bare_sink()
    server.send_home(s)
    assert s._cmd_q[0] == "hold"
    assert s._cmd_q[1]["cmd"] == "home"
    assert s._cmd_q[1]["hands"]["left"] == pytest.approx(rg.grip_q("left", 1.0))


def test_the_control_page_home_carries_the_hands(thumb0):
    rg.set_grip_thumb0(1.0)
    s = bare_sink()
    app = {"sink": s, "control_key": KEY}
    resp = asyncio.run(server.control_handler(_req(app, {"cmd": "home"}, key=KEY)))
    assert resp.status == 200
    homes = [c for c in s._cmd_q if isinstance(c, dict) and c.get("cmd") == "home"]
    assert len(homes) == 1 and homes[0]["hands"]["right"] == pytest.approx(rg.grip_q("right", 1.0))


def test_the_stick_home_carries_the_hands(thumb0):
    rg.set_grip_thumb0(1.0)
    s = bare_sink()
    stats = {}
    s.onboard_state = "ARMED"
    frame = {"hands": [{"hand": side, "source": "controller",
                         "buttons": {"stick": True}} for side in ("left", "right")]}
    assert server.arm_on_sticks(frame, stats, {"sink": s}) is None
    homes = [c for c in s._cmd_q if isinstance(c, dict) and c.get("cmd") == "home"]
    assert len(homes) == 1 and "hands" in homes[0]


# ---- the knob survives a redeploy ----------------------------------------------

def test_grip_thumb0_from_env(thumb0):
    assert server.grip_thumb0_from_env({}) is None
    assert server.grip_thumb0_from_env({"TELEOP_GRIP_THUMB0": "-0.5"}) == -0.5
    assert rg.GRIP_THUMB0 == -0.5, "applied to the live knob"
    with pytest.raises(ValueError):
        server.grip_thumb0_from_env({"TELEOP_GRIP_THUMB0": "2"})
    with pytest.raises(ValueError):
        server.grip_thumb0_from_env({"TELEOP_GRIP_THUMB0": "nan"})


# ---- the onboard uses what it was sent -----------------------------------------

def _holding_commander():
    c = ob.Commander()
    c.arm, c.hand = FakeArm(), FakeHand()
    c.state = "HOLDING"
    c.home_q = [0.0] * ob.ARM_DIM
    c.measured_arm_q = [0.0] * ob.ARM_DIM
    return c


def test_go_home_closes_the_hands_to_the_pose_it_was_sent():
    c = _holding_commander()
    left = list(ds.CLOSED_LEFT)
    right = list(ds.CLOSED_RIGHT)
    left[0], right[0] = -1.0, 1.0             # the thumb across the palm
    reply = c.go_home({"left": left, "right": right})
    assert c.hand.grips, "the hands were commanded"
    l, r = c.hand.grips[-1]
    assert l == pytest.approx(left) and r == pytest.approx(right)
    assert "bridge grip" in reply


def test_go_home_falls_back_to_its_own_closed_pose():
    c = _holding_commander()
    reply = c.go_home()                       # no payload: an older bridge
    l, r = c.hand.grips[-1]
    assert l == pytest.approx(ds.CLOSED_LEFT) and r == pytest.approx(ds.CLOSED_RIGHT)
    assert "onboard default" in reply
    c2 = _holding_commander()
    reply2 = c2.go_home({"left": [float("nan")] * 7, "right": [0.0] * 7})
    assert c2.hand.grips[-1][0] == pytest.approx(ds.CLOSED_LEFT)
    assert "onboard default" in reply2


def test_go_home_clamps_a_pose_past_the_range():
    c = _holding_commander()
    right = list(ds.CLOSED_RIGHT)
    right[0] = 9.0
    c.go_home({"left": list(ds.CLOSED_LEFT), "right": right})
    assert c.hand.grips[-1][1][0] == pytest.approx(ds.RANGE_RIGHT[0][1])


def test_the_onboard_dispatch_passes_the_hands_to_go_home():
    """The wire form: {"cmd": "home", "hands": {...}} reaches go_home(hands)."""
    src = open(os.path.join(ROOT, "onboard", "g1_onboard.py")).read()
    assert 'cmd.go_home, m.get("hands")' in src
