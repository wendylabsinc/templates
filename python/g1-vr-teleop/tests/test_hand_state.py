"""Read more than position off the Dex3, and write it into every row.

The knuckles fell to 12-17 % follow and nothing recorded could say why:
measured() kept `q` alone. HandState_ carries temperature, current, mode and
an error word per motor, and power/error per hand. A rising temperature says
thermal; a non-zero motorstate says latched; neither can be seen until it is
read. The mode byte is also rewritten every frame, by analogy with the kp bug
(#43): written once at construction, a latched motor stays latched.
"""
import asyncio
import gzip
import json
import os
import sys
import time
import types

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "teleop", "bridge"))
sys.path.insert(0, os.path.join(ROOT, "onboard"))

import episode_check  # noqa: E402
import sink_realg1 as sr  # noqa: E402
from tests.test_dex3_order import dex3, BRIDGE_ORDER  # noqa: E402,F401
from tests.test_sink_ack import bare_sink, FakeWs  # noqa: E402


def _motor(q=0.0, dq=0.0, tau=0.0, temp=(30, 31), mode=1, motorstate=0):
    return types.SimpleNamespace(mode=mode, q=q, dq=dq, ddq=0.0, tau_est=tau,
                                 temperature=list(temp), vol=24.0, sensor=[0, 0],
                                 motorstate=motorstate, reserve=[0, 0, 0, 0])


def _hand_state(motors, power_a=0.4, error=(0, 0)):
    return types.SimpleNamespace(motor_state=motors, press_sensor_state=[], imu_state=None,
                                 power_v=12.0, power_a=power_a, system_v=5.0, device_v=5.0,
                                 error=list(error), reserve=[0, 0])


# ---- the sender ----------------------------------------------------------------

def test_measured_state_carries_temperature_current_mode_and_error(dex3):
    s = dex3.Dex3Sender()
    hw = [_motor(q=0.1 * i, dq=0.01 * i, tau=0.2, temp=(40 + i, 38)) for i in range(7)]
    hw[int(dex3.Dex3_1_Right_JointIndex.kRightHandMiddle0)].motorstate = 7
    hw[int(dex3.Dex3_1_Right_JointIndex.kRightHandMiddle0)].temperature = [72, 60]
    dex3._FakeSub.latest = _hand_state(hw, power_a=1.5, error=(0, 3))
    st = s.measured_state()["right"]
    i = BRIDGE_ORDER.index("middle_0")
    assert st["q"][i] == pytest.approx(hw[int(dex3.Dex3_1_Right_JointIndex.kRightHandMiddle0)].q)
    assert st["temperature"][i] == 72, "the hotter of the two sensors"
    assert st["motorstate"][i] == 7
    assert st["dq"][i] == pytest.approx(hw[int(dex3.Dex3_1_Right_JointIndex.kRightHandMiddle0)].dq)
    assert st["tau_est"][i] == pytest.approx(0.2)
    assert st["mode"][i] == 1
    assert st["power_a"] == pytest.approx(1.5) and st["error"] == [0, 3]
    assert s.measured()["right"] == st["q"], "measured() keeps its shape"


def test_missing_attributes_read_as_none_not_zero(dex3):
    s = dex3.Dex3Sender()
    dex3._FakeSub.latest = types.SimpleNamespace(
        motor_state=[types.SimpleNamespace(q=0.5) for _ in range(7)])
    st = s.measured_state()["right"]
    assert st["q"] == [0.5] * 7
    assert st["temperature"] is None and st["motorstate"] is None and st["power_a"] is None


def test_the_mode_byte_is_rewritten_every_frame(dex3):
    s = dex3.Dex3Sender()
    want = {jid: s._left.motor_cmd[jid].mode for jid in range(7)}
    assert all(v != 0 for v in want.values())
    for jid in range(7):
        s._left.motor_cmd[jid].mode = 0           # as a latched motor would see it
    s.ctrl_dual_hand([0.0] * 7, [0.0] * 7)
    assert {jid: s._left.motor_cmd[jid].mode for jid in range(7)} == want


# ---- the onboard ack -----------------------------------------------------------

def test_the_ack_carries_hand_state_and_backoff(dex3):
    import g1_onboard as ob
    from tests.test_onboard_safety import armed_commander

    class _Hand:
        def measured(self):
            return {"left": [0.0] * 7, "right": [0.1] * 7}

        def measured_state(self):
            return {"left": None,
                    "right": {"q": [0.1] * 7, "dq": [0.0] * 7, "tau_est": None,
                              "temperature": [50] * 7, "mode": [1] * 7, "motorstate": [0] * 7,
                              "power_v": 12.0, "power_a": 0.3, "error": [0, 0]}}

    c = armed_commander()
    c.hand = _Hand()
    c._hand_backoff_names = {"left": [], "right": ["middle_0"]}
    ack = ob.ack_message(c, 3, True, [])
    assert ack["hands"] == {"left": [0.0] * 7, "right": [0.1] * 7}, "unchanged"
    assert ack["hand_state"]["right"]["temperature"] == [50] * 7
    assert ack["hand_state"]["left"] is None
    assert ack["hand_backoff"] == {"left": [], "right": ["middle_0"]}


def test_apply_backs_off_a_stalled_knuckle_and_names_it():
    import g1_onboard as ob
    import dex3_stall as ds
    from tests.test_onboard_safety import armed_commander

    class _Hand:
        def __init__(self):
            self.grips = []
            self.q = {"left": [0.0] * 7, "right": [0.0] * 7}

        def ctrl_dual_hand(self, left, right):
            self.grips.append((list(left), list(right)))

        def measured(self):
            return dict(self.q)

    clk = {"t": 100.0}
    c = armed_commander()
    c.hand = _Hand()
    c._stall = ds.StallBackoff(clock=lambda: clk["t"])
    i = BRIDGE_ORDER.index("middle_0")
    closed = [0.0] * 7
    closed[i] = 1.5
    c.hand.q["right"][i] = 0.4                # stopped on the can
    for _ in range(3):
        c.last_apply_t = 0.0
        assert c.apply([0.0] * ob.ARM_DIM, [0.0] * 7, closed)
        clk["t"] += 0.4
    # measured 0.4 + the guard's own hold margin, read from the guard: the
    # grip force is tuned against a real can and 0.35 is no longer the answer.
    import dex3_stall as _ds
    assert c.hand.grips[-1][1][i] == pytest.approx(0.4 + _ds.StallBackoff().margin_rad)
    assert c._hand_backoff_names == {"left": [], "right": ["middle_0"]}
    assert c.hand.grips[0][1][i] == pytest.approx(1.5), "not before the hold time"


# ---- the bridge ----------------------------------------------------------------

def test_the_sink_keeps_hand_state_and_backoff_from_the_ack():
    s = bare_sink()
    s.last_hand_q = None
    state = {"left": None, "right": {"q": [0.1] * 7, "temperature": [61] * 7,
                                     "motorstate": [0] * 7, "error": [0, 0], "power_a": 0.2}}
    asyncio.run(s._read(FakeWs([{"ack": 1, "state": "ARMED", "applied": True,
                                 "hands": {"left": None, "right": [0.1] * 7},
                                 "hand_state": state,
                                 "hand_backoff": {"left": [], "right": ["index_0"]}}])))
    v = s.robot_view()
    assert v["hand_state"] == state
    assert v["hand_backoff"] == {"left": [], "right": ["index_0"]}
    st = s.status()
    assert st["hand_temp_max_c"] == 61 and st["hand_error"] is False


def test_an_old_onboard_gives_none_for_hand_state():
    s = bare_sink()
    s.last_hand_q = None
    asyncio.run(s._read(FakeWs([{"ack": 1, "state": "ARMED", "applied": True}])))
    v = s.robot_view()
    assert v["hand_state"] is None and v["hand_backoff"] is None
    assert s.status()["hand_temp_max_c"] is None


# ---- the validator -------------------------------------------------------------

def _write(tmp_path, rows, name="20260916-120000-hands"):
    p = tmp_path / f"{name}.jsonl"
    t0 = 1000.0
    lines = [{"type": "header", "session": name, "t_start": t0, "v": 1, "arm_mode": "geometry"}]
    for i, r in enumerate(rows):
        r = dict(r)
        r["t"] = t0 + 0.011 * i
        lines.append(r)
    lines.append({"type": "footer", "frames": len(rows), "dropped": 0, "marks": 0,
                  "t_end": t0 + 0.011 * len(rows)})
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return p


def _hand_row(cmd_frac, meas_frac, temp=35, motorstate=0, error=(0, 0)):
    i0 = BRIDGE_ORDER.index("middle_0")
    q = [0.0] * 7
    q[i0] = 1.5 * meas_frac
    tg = {"right_hand_middle_0_joint": 1.5 * cmd_frac, "right_elbow_joint": 0.3,
          "left_elbow_joint": 0.3}
    return {"hands": [{"hand": "right"}, {"hand": "left"}], "head": None, "targets": tg,
            "robot": {"state": "ARMED", "q": [0.3] * 14, "q_age_s": 0.01, "applied": True,
                      "hand_q": {"left": [0.0] * 7, "right": q},
                      "hand_state": {"left": None,
                                     "right": {"temperature": [temp] * 7,
                                               "motorstate": [motorstate] * 7,
                                               "error": list(error)}}}}


def test_a_knuckle_that_degrades_over_the_session_is_flagged(tmp_path):
    rows = []
    for k in range(2400):
        cmd = 1.0 if (k // 20) % 2 else 0.0    # open/close cycling
        follow = 1.0 if k < 800 else 0.15       # then the knuckle stops following
        rows.append(_hand_row(cmd, cmd * follow))
    rep = episode_check.check(_write(tmp_path, rows))
    codes = {i["code"] for i in rep["issues"]}
    assert "hand_derate" in codes
    detail = next(i for i in rep["issues"] if i["code"] == "hand_derate")["detail"]
    assert "right_hand_middle_0_joint" in detail and "15" in detail


def test_a_steady_knuckle_is_not_flagged(tmp_path):
    rows = [_hand_row(1.0 if (k // 20) % 2 else 0.0, (1.0 if (k // 20) % 2 else 0.0) * 0.97)
            for k in range(2400)]
    rep = episode_check.check(_write(tmp_path, rows))
    assert "hand_derate" not in {i["code"] for i in rep["issues"]}


def test_a_hot_hand_and_a_motor_error_are_flagged(tmp_path):
    rows = [_hand_row(1.0, 1.0, temp=64) for _ in range(50)]
    rep = episode_check.check(_write(tmp_path, rows))
    codes = {i["code"]: i["severity"] for i in rep["issues"]}
    assert codes.get("hand_hot") == "warn"
    rows = [_hand_row(1.0, 1.0, motorstate=3) for _ in range(50)]
    rep = episode_check.check(_write(tmp_path, rows))
    codes = {i["code"]: i["severity"] for i in rep["issues"]}
    assert codes.get("hand_error") == "error"
    assert rep["hand_temp_max_c"] == 35
