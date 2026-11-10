"""Episodes carry the measured state a physics replay needs: waist + arms, with velocities.

Until now the onboard kept 14 arm positions from rt/lowstate and nothing
else: no velocities, no waist, no torque. That is enough to say where the arm
was and not enough to say how it moved. Replaying a recorded command stream
into a physics simulator and diffing against the real robot -- the direct
measurement of the sim-to-real gap the training side has been trying to
establish for weeks -- needs dq, and the simulator's 43-joint model includes
the waist. So the onboard now carries Unitree motors 12..28 (waist yaw/roll/
pitch, then the 14 arm joints) with q, dq and tau_est in every reply, the sink
keeps the newest, and the recorder writes it under robot.body. Everything that
existed before (`q`, the 14-vector) is unchanged.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "onboard"))
sys.path.insert(0, os.path.join(ROOT, "teleop", "bridge"))

import g1_onboard as ob  # noqa: E402
import episode_check  # noqa: E402
from session_recorder import SessionRecorder  # noqa: E402
from tests.test_onboard_safety import armed_commander  # noqa: E402
from tests.test_record_onboard_view import _sink, _Stream  # noqa: E402

BODY_IDX = list(range(12, 29))


class _Motor:
    def __init__(self, i):
        self.q = 0.01 * i
        self.dq = 0.1 * i
        self.tau_est = 1.0 * i
        self.temperature = 30 + i
        self.ddq = 0.0


class _LowState:
    """35 motor slots like the HG LowState, a tick, and no remote payload."""
    def __init__(self, tick=1234):
        self.motor_state = [_Motor(i) for i in range(35)]
        self.tick = tick
        self.wireless_remote = None


def _drain(r, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not r.status()["queued"]:
            return True
        time.sleep(0.01)
    return False


# ---- onboard ------------------------------------------------------------------

def test_lowstate_keeps_waist_and_arm_q_dq_tau_in_unitree_order():
    c = armed_commander()
    c.n_lowstate = 0
    c.on_lowstate(_LowState(tick=777))
    b = c.measured_body
    assert b["idx"] == BODY_IDX
    assert b["q"] == pytest.approx([0.01 * i for i in BODY_IDX])
    assert b["dq"] == pytest.approx([0.1 * i for i in BODY_IDX])
    assert b["tau"] == pytest.approx([1.0 * i for i in BODY_IDX])
    assert b["temperature"] == [30 + i for i in BODY_IDX]
    assert b["tick"] == 777
    assert isinstance(b["t"], float)
    # The 14-vector everyone already reads is exactly the arm slice, unchanged.
    assert c.measured_arm_q == pytest.approx([0.01 * i for i in range(15, 29)])


def test_lowstate_keeps_temperature_for_every_active_body_motor():
    c = armed_commander()
    c.n_lowstate = 0
    c.on_lowstate(_LowState(tick=778))
    t = c.measured_motor_temperature
    assert t["idx"] == list(range(29))
    assert len(t["names"]) == 29
    assert t["temperature_c"] == list(range(30, 59))
    assert t["tick"] == 778
    assert isinstance(t["t"], float)


def test_temperature_keeps_partial_measurement_and_marks_missing_motor():
    c = armed_commander()
    c.n_lowstate = 0
    msg = _LowState()
    del msg.motor_state[4].temperature
    msg.motor_state[8].temperature = [31, 36]
    c.on_lowstate(msg)
    t = c.measured_motor_temperature
    assert t["temperature_c"][4] is None
    assert t["temperature_c"][8] == 36


def test_lowstate_without_tau_or_tick_still_records_q_and_dq():
    """An older IDL or a message without tau_est must not cost the rest."""
    c = armed_commander()
    c.n_lowstate = 0
    msg = _LowState()
    for m in msg.motor_state:
        del m.tau_est
    del msg.tick
    c.on_lowstate(msg)
    b = c.measured_body
    assert b["q"] == pytest.approx([0.01 * i for i in BODY_IDX])
    assert b["dq"] == pytest.approx([0.1 * i for i in BODY_IDX])
    assert b["tau"] is None and b["tick"] is None


def test_ack_and_idle_messages_carry_body_rounded_and_q_unchanged():
    c = armed_commander()
    c.n_lowstate = 0
    c.on_lowstate(_LowState())
    ack = ob.ack_message(c, 3, True, [])
    idle = ob.idle_q_message(c)
    for m in (ack, idle):
        assert m["q"] == [round(0.01 * i, 5) for i in range(15, 29)], "q is what it always was"
        body = m["body"]
        assert body["idx"] == BODY_IDX
        assert body["q"] == [round(0.01 * i, 5) for i in BODY_IDX]
        assert body["dq"] == [round(0.1 * i, 5) for i in BODY_IDX]
        assert body["tau"] == [round(1.0 * i, 3) for i in BODY_IDX]
        assert body["temperature"] == [30 + i for i in BODY_IDX]
        assert body["tick"] == 1234
        assert body["age_s"] is not None and body["age_s"] < 1.0
        temp = m["motor_temperature"]
        assert temp["idx"] == list(range(29))
        assert temp["temperature_c"] == list(range(30, 59))
        assert temp["max_c"] == 58
        assert temp["hottest"] == [temp["names"][-1]]
        assert temp["age_s"] is not None and temp["age_s"] < 1.0
    assert json.dumps(ack)  # it goes on the wire


def test_lowstate_without_temperature_keeps_the_rest_of_the_body_state():
    c = armed_commander()
    c.n_lowstate = 0
    msg = _LowState()
    for m in msg.motor_state:
        del m.temperature
    c.on_lowstate(msg)
    assert c.measured_body["temperature"] is None
    assert ob.ack_message(c, 1, True)["body"]["temperature"] is None


def test_body_is_none_before_any_lowstate():
    c = armed_commander()
    assert ob.ack_message(c, 1, False, None)["body"] is None
    assert ob.idle_q_message(c)["body"] is None
    assert ob.ack_message(c, 1, False, None)["motor_temperature"] is None
    assert ob.idle_q_message(c)["motor_temperature"] is None


# ---- bridge sink ------------------------------------------------------------------

def _body(v=0.5):
    return {"idx": BODY_IDX, "q": [v] * 17, "dq": [0.0] * 17, "tau": [0.1] * 17,
            "tick": 9, "age_s": 0.004}


def test_sink_keeps_the_newest_body_and_the_view_exposes_it_with_age():
    s = _sink()
    asyncio.run(s._read(_Stream([
        {"ack": 1, "state": "ARMED", "applied": True, "q": [0.3] * 14, "body": _body(0.5)},
        {"state": "ARMED", "q": [0.4] * 14, "body": _body(0.6)},   # idle stream, newest wins
    ])))
    v = s.robot_view()
    assert v["q"] == pytest.approx([0.4] * 14)
    assert v["body"]["idx"] == BODY_IDX
    assert v["body"]["q"] == pytest.approx([0.6] * 17)
    assert v["body"]["dq"] == pytest.approx([0.0] * 17)
    assert v["body"]["tau"] == pytest.approx([0.1] * 17)
    assert v["body"]["tick"] == 9
    assert 0.0 <= v["body"]["age_s"] < 1.0, "age since the BRIDGE received it"


def test_sink_keeps_all_motor_temperatures_for_recording_and_benchmarking():
    s = _sink()
    temp = {"idx": list(range(29)), "names": [f"joint_{i}" for i in range(29)],
            "temperature_c": list(range(30, 59)), "tick": 10, "age_s": 0.004,
            "max_c": 58, "hottest": ["joint_28"]}
    asyncio.run(s._read(_Stream([
        {"state": "ARMED", "q": [0.4] * 14, "motor_temperature": temp},
    ])))
    view = s.robot_view()
    assert view["motor_temperature"]["temperature_c"] == list(range(30, 59))
    assert view["motor_temperature"]["max_c"] == 58
    assert 0.0 <= view["motor_temperature"]["age_s"] < 1.0
    status = s.status()
    assert status["motor_temperature"]["temperature_c"] == list(range(30, 59))
    assert status["body_temp_hottest"] == ["joint_28"]
    assert status["motor_heat_alert"] == {
        "threshold_c": 80.0, "active": False,
        "joint": "joint_28", "temperature_c": 58.0,
    }


def test_sink_alert_names_the_hottest_motor_across_body_and_hands():
    s = _sink()
    s.onboard_motor_temperature = {
        "names": ["left_elbow_joint", "right_elbow_joint"],
        "temperature_c": [79, 80],
    }
    s.last_hand_state = {
        "left": {"temperature": [81] + [45] * 6},
        "right": {"temperature": [50] * 7},
    }
    alert = s.status()["motor_heat_alert"]
    assert alert == {
        "threshold_c": 80.0, "active": True,
        "joint": "left_hand_thumb_0_joint", "temperature_c": 81.0,
    }


def test_sink_without_body_from_an_old_onboard_says_none():
    s = _sink()
    asyncio.run(s._read(_Stream([
        {"ack": 1, "state": "ARMED", "applied": True, "q": [0.3] * 14},
    ])))
    v = s.robot_view()
    assert v["q"] == pytest.approx([0.3] * 14)
    assert v["body"] is None


def test_sink_ignores_a_malformed_body():
    s = _sink()
    asyncio.run(s._read(_Stream([
        {"state": "ARMED", "q": [0.3] * 14, "body": {"idx": BODY_IDX, "q": [1.0] * 3}},
        {"state": "ARMED", "q": [0.3] * 14, "body": "nope"},
    ])))
    assert s.robot_view()["body"] is None


# ---- recorder -------------------------------------------------------------------

def test_recorder_writes_robot_body_through(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    sid = rec.start("b")["session"]
    rec.note({"t": 1.0, "hands": []}, {"left_elbow_joint": 0.1},
             {"state": "ARMED", "q": [0.0] * 14, "body": _body()})
    assert _drain(rec)
    rec.stop()
    rows = [json.loads(l) for l in open(tmp_path / f"{sid}.jsonl") if l.strip()]
    assert rows[1]["robot"]["body"]["idx"] == BODY_IDX
    assert len(rows[1]["robot"]["body"]["dq"]) == 17


# ---- validator ------------------------------------------------------------------

def _session(tmp_path, with_body):
    p = tmp_path / "20260916-000000-body.jsonl"
    rows = [{"type": "header", "session": "20260916-000000-body", "t_start": 0.0, "v": 1}]
    for i in range(40):
        robot = {"state": "ARMED", "q": [0.0] * 14, "q_age_s": 0.01, "applied": True}
        if with_body:
            robot["body"] = _body()
        rows.append({"t": i * 0.05, "hands": [], "head": None, "robot": robot,
                     "targets": {"left_elbow_joint": 0.1}})
    rows.append({"type": "footer", "frames": 40, "dropped": 0, "t_end": 2.0})
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def test_check_warns_when_body_state_is_absent(tmp_path):
    rep = episode_check.check(_session(tmp_path, with_body=False))
    assert rep["body_rows"] == 0
    assert "no_body_state" in [i["code"] for i in rep["issues"]]


def test_check_reports_body_state_coverage(tmp_path):
    rep = episode_check.check(_session(tmp_path, with_body=True))
    assert rep["body_rows"] == 40
    assert "no_body_state" not in [i["code"] for i in rep["issues"]]
    assert "body" in episode_check.summary(rep)


def test_the_real_first_episode_is_read_and_has_no_body_state():
    """The committed-but-uncommitted first real recording predates this;
    the check must read it and say so rather than choke on it."""
    p = os.path.join(ROOT, "episodes", "20260915-154211-coke-demo-1.jsonl.gz")
    if not os.path.exists(p):
        pytest.skip("first real episode not present in this checkout")
    rep = episode_check.check(p)
    assert rep["body_rows"] == 0
    assert "no_body_state" in [i["code"] for i in rep["issues"]]
