"""Held-X fore/aft controller gain before IK, without changing raw input."""
import copy
import json
import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))

from controller_wrist_gain import ControllerWristGain  # noqa: E402
from session_recorder import SessionRecorder  # noqa: E402


def _hand(side, wrist, *, x=False, source="controller"):
    return {"hand": side, "source": source, "wrist": list(wrist),
            "quat": [0.1, 0.2, 0.3, 0.9], "trigger": 0.4,
            "buttons": {"primary": bool(x), "secondary": False}}


def _frame(left, right=None):
    return {"head": {"pos": [0, 1.6, 0]},
            "hands": [left] + ([right] if right is not None else [])}


def _gain(**kw):
    return ControllerWristGain(backward_gain=1.5, forward_gain=0.35,
                               deadband_m=0.0, max_velocity_m_s=100.0,
                               max_dt_s=0.1, **kw)


def test_x_press_captures_each_controller_without_moving_the_first_frame():
    raw = _frame(_hand("left", [-0.2, -0.1, -0.4], x=True),
                 _hand("right", [0.2, -0.1, -0.4]))
    original = copy.deepcopy(raw)
    out, obs = _gain().transform(raw, armed=True, now=1.0)

    assert raw == original, "the raw recording input was mutated"
    assert [h["wrist"] for h in out["hands"]] == [h["wrist"] for h in raw["hands"]]
    assert obs["active"] is True
    assert obs["sides"]["left"]["captured_raw_wrist"] == [-0.2, -0.1, -0.4]
    assert obs["sides"]["right"]["captured_raw_wrist"] == [0.2, -0.1, -0.4]


def test_backward_is_amplified_and_forward_is_attenuated_incrementally():
    g = _gain()
    g.transform(_frame(_hand("left", [0, 0, -0.4], x=True)), armed=True, now=1.0)
    back, _ = g.transform(_frame(_hand("left", [0, 0, -0.3], x=True)),
                          armed=True, now=1.1)
    assert back["hands"][0]["wrist"][2] == pytest.approx(-0.25)  # +0.10 * 1.5

    forward, _ = g.transform(_frame(_hand("left", [0, 0, -0.4], x=True)),
                             armed=True, now=1.2)
    assert forward["hands"][0]["wrist"][2] == pytest.approx(-0.285)  # -0.10 * .35


def test_only_fore_aft_position_changes():
    g = _gain()
    g.transform(_frame(_hand("left", [0.1, 0.2, -0.4], x=True)), armed=True, now=1.0)
    raw = _frame(_hand("left", [0.7, -0.8, -0.3], x=True))
    out, _ = g.transform(raw, armed=True, now=1.1)
    got, want = out["hands"][0], raw["hands"][0]
    assert got["wrist"][:2] == want["wrist"][:2]
    assert got["quat"] == want["quat"]
    assert got["trigger"] == want["trigger"]
    assert got["buttons"] == want["buttons"]


def test_directional_deadband_accumulates_slow_motion_but_rejects_jitter():
    g = ControllerWristGain(backward_gain=1.5, forward_gain=0.35,
                            deadband_m=0.003, max_velocity_m_s=100.0,
                            max_dt_s=0.1)
    g.transform(_frame(_hand("left", [0, 0, -0.4], x=True)), armed=True, now=1.0)
    for i, z in enumerate((-0.399, -0.4, -0.399), 1):
        out, _ = g.transform(_frame(_hand("left", [0, 0, z], x=True)),
                             armed=True, now=1.0 + i * 0.01)
        assert out["hands"][0]["wrist"][2] == pytest.approx(-0.4)
    out, _ = g.transform(_frame(_hand("left", [0, 0, -0.396], x=True)),
                         armed=True, now=1.05)
    assert out["hands"][0]["wrist"][2] > -0.4, "slow deliberate motion was lost"


def test_amplified_motion_is_velocity_bounded():
    g = ControllerWristGain(backward_gain=1.5, forward_gain=0.35,
                            deadband_m=0.0, max_velocity_m_s=0.2,
                            max_dt_s=0.1)
    g.transform(_frame(_hand("left", [0, 0, -0.4], x=True)), armed=True, now=1.0)
    out, obs = g.transform(_frame(_hand("left", [0, 0, 0.6], x=True)),
                           armed=True, now=1.1)
    assert out["hands"][0]["wrist"][2] == pytest.approx(-0.38)
    assert obs["sides"]["left"]["velocity_limited"] is True


def test_x_release_rebases_then_normal_motion_continues_one_to_one_without_a_snap():
    g = _gain()
    g.transform(_frame(_hand("left", [0, 0, -0.4], x=True)), armed=True, now=1.0)
    held, _ = g.transform(_frame(_hand("left", [0, 0, -0.3], x=True)),
                          armed=True, now=1.1)
    held_z = held["hands"][0]["wrist"][2]

    released, obs = g.transform(_frame(_hand("left", [0, 0, -0.3], x=False)),
                                armed=True, now=1.2)
    assert released["hands"][0]["wrist"][2] == pytest.approx(held_z)
    assert obs["active"] is False and obs["released"] is True

    normal, _ = g.transform(_frame(_hand("left", [0, 0, -0.28], x=False)),
                            armed=True, now=1.3)
    assert normal["hands"][0]["wrist"][2] == pytest.approx(held_z + 0.02)


def test_release_rebases_a_temporarily_missing_controller_at_its_last_pose():
    g = _gain()
    both = _frame(_hand("left", [-0.2, 0, -0.4], x=True),
                  _hand("right", [0.2, 0, -0.4]))
    g.transform(both, armed=True, now=1.0)
    moved = _frame(_hand("left", [-0.2, 0, -0.3], x=True),
                   _hand("right", [0.2, 0, -0.3]))
    held, _ = g.transform(moved, armed=True, now=1.1)
    held_right_z = held["hands"][1]["wrist"][2]

    # Right tracking drops exactly when X is released.
    g.transform(_frame(_hand("left", [-0.2, 0, -0.3], x=False)),
                armed=True, now=1.2)
    returned, _ = g.transform(
        _frame(_hand("left", [-0.2, 0, -0.3], x=False),
               _hand("right", [0.2, 0, -0.3])),
        armed=True, now=1.3)
    assert returned["hands"][1]["wrist"][2] == pytest.approx(held_right_z)


def test_non_controller_hands_and_unarmed_frames_are_unchanged():
    g = _gain()
    hand = _hand("left", [0, 0, -0.4], x=True, source="hand")
    out, obs = g.transform(_frame(hand), armed=True, now=1.0)
    assert out == _frame(hand) and obs["active"] is False

    controller = _frame(_hand("left", [0, 0, -0.4], x=True))
    out, obs = g.transform(controller, armed=False, now=2.0)
    assert out == controller and obs["active"] is False


def test_environment_configures_initial_directional_values():
    g = ControllerWristGain.from_env({
        "TELEOP_ARM_BACKWARD_GAIN": "1.7",
        "TELEOP_ARM_FORWARD_GAIN": "0.25",
        "TELEOP_ARM_DIRECTION_DEADBAND_M": "0.004",
        "TELEOP_ARM_DIRECTION_MAX_M_S": "0.5",
    })
    assert g.backward_gain == 1.7 and g.forward_gain == 0.25
    assert g.deadband_m == 0.004 and g.max_velocity_m_s == 0.5


def test_recorder_keeps_raw_wrist_and_versioned_derived_observation(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    raw = _frame(_hand("left", [0, 0, -0.3], x=True))
    derived = {
        "schema": "wendy.g1.controller-wrist-gain.v1",
        "active": True,
        "sides": {"left": {"effective_wrist": [0, 0, -0.25]}},
    }
    try:
        sid = rec.start("x-gain")["session"]
        rec.note(raw, {"left_elbow_joint": 0.2}, {"state": "ARMED"},
                 wrist_gain=derived)
        deadline = time.time() + 2
        while rec.status()["queued"] and time.time() < deadline:
            time.sleep(0.01)
        rec.stop()
        rows = [json.loads(line) for line in pathlib.Path(rec.path(sid)).read_text().splitlines()]
    finally:
        rec.close()

    assert rows[1]["hands"][0]["wrist"] == [0, 0, -0.3]
    assert rows[1]["controller_wrist_gain"] == derived


def test_server_applies_gain_before_ik_and_records_raw_frame():
    source = (ROOT / "teleop" / "bridge" / "server.py").read_text()
    loop = source.split("async def ws_handler")[1].split("\nasync def ")[0]
    transform = loop.index("controller_wrist_gain.transform(")
    solve = loop.index("retargeter.process(control_frame)")
    record = loop.index("session.note(frame")
    assert transform < solve < record
    assert "wrist_gain=wrist_gain_observation" in loop
    assert "controller_wrist_gain" in source.split("async def control_status_handler")[1]


# ---- off by default, at the operator's request ------------------------------------

def test_held_x_is_disabled_and_no_environment_turns_it_on():
    assert ControllerWristGain.from_env({}).enabled is False
    for v in ("1", "true"):
        assert ControllerWristGain.from_env({"TELEOP_ARM_X_GAIN": v}).enabled is False


def test_off_holding_x_and_moving_back_passes_the_controller_through_one_to_one():
    g = ControllerWristGain.from_env({})
    first = _frame(_hand("left", [0.0, 0.0, 0.0], x=True))
    out, obs = g.transform(first, armed=True, now=0.0)
    assert out is first and obs["enabled"] is False and obs["active"] is False
    moved = _frame(_hand("left", [0.0, 0.0, 0.10], x=True))
    out, _ = g.transform(moved, armed=True, now=0.02)
    assert out["hands"][0]["wrist"] == [0.0, 0.0, 0.10], "no backward boost"


def test_x_is_not_in_the_legend():
    root = pathlib.Path(__file__).resolve().parents[1]
    hud = (root / "teleop" / "web" / "hud.js").read_text().split("const rows = [")[1].split("];")[0]
    assert "X (hold)" not in hud
