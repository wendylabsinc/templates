"""Per-joint stiffness from the jog panel, and the per-frame ack.

The whole safety story for gains is bounds plus a ramp, so that is what is
pinned: out-of-range refused, unknown joints refused, a change applied in
steps rather than one, and hold() restoring the commander's table rather than
the controller's constructor values (so a stiffness set from the panel
survives a limp/hold cycle). The ack must say whether the frame was applied,
not merely read.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "onboard"))

import g1_onboard as ob  # noqa: E402


class GainArm:
    """A controller that can set gains per joint and remembers every write."""
    def __init__(self):
        self.q = [0.1] * ob.ARM_DIM
        self.writes = []                 # (idx, kp, kd)
        self.commands = []
        self.compliance = []

    def default_gains(self):
        return [(40.0, 1.5) if "wrist" in ob.ARM_JOINT_ORDER[i] else (80.0, 3.0)
                for i in range(ob.ARM_DIM)]

    def set_joint_gains(self, idx, kp, kd):
        self.writes.append((int(idx), float(kp), float(kd)))

    def set_arm_compliance(self, kp=None, kd=None):
        self.compliance.append((kp, kd))

    def get_current_dual_arm_q(self):
        return list(self.q)

    def ctrl_dual_arm(self, q, tau):
        self.commands.append(list(q))


@pytest.fixture
def cmd(tmp_path, monkeypatch):
    monkeypatch.setattr(ob, "GAINS_PATH", str(tmp_path / "gains.json"))
    c = ob.Commander()
    c.arm = GainArm()
    c.state = "HOLDING"
    with c.lock:
        c._init_gains(c.arm)
    return c


def test_gains_start_from_the_controller_defaults(cmd):
    assert cmd.gains == cmd.arm.default_gains()
    rep = cmd.gains_report()
    assert rep["left_elbow_joint"] == {"kp": 80.0, "kd": 3.0}
    assert rep["left_wrist_roll_joint"] == {"kp": 40.0, "kd": 1.5}


def test_out_of_range_is_refused_and_nothing_is_written(cmd):
    r = cmd.set_gains({"left_elbow_joint": {"kp": 151}}, ramp_s=0.0)
    assert r.startswith("refused") and "151" in r
    r = cmd.set_gains({"left_wrist_roll_joint": {"kp": 81}}, ramp_s=0.0)
    assert r.startswith("refused")
    r = cmd.set_gains({"left_elbow_joint": {"kd": 5.5}}, ramp_s=0.0)
    assert r.startswith("refused")
    r = cmd.set_gains({"left_elbow_joint": {"kp": -1}}, ramp_s=0.0)
    assert r.startswith("refused")
    assert cmd.arm.writes == []


def test_unknown_or_non_numeric_is_refused(cmd):
    assert cmd.set_gains({"left_hip_joint": {"kp": 10}}, ramp_s=0.0).startswith("refused")
    assert cmd.set_gains({"left_elbow_joint": {"kp": "hard"}}, ramp_s=0.0).startswith("refused")
    assert cmd.set_gains({"left_elbow_joint": {"kp": float("nan")}}, ramp_s=0.0).startswith("refused")
    assert cmd.set_gains("everything", ramp_s=0.0).startswith("refused")
    assert cmd.arm.writes == []


def test_a_change_is_ramped_not_stepped(cmd):
    t0 = time.monotonic()
    r = cmd.set_gains({"left_elbow_joint": {"kp": 120.0, "kd": 4.0}}, ramp_s=0.1)
    dt = time.monotonic() - t0
    assert r.startswith("gains set on 1 joint")
    idx = ob.ARM_JOINT_INDEX["left_elbow_joint"]
    w = [x for x in cmd.arm.writes if x[0] == idx]
    assert len(w) == ob.GAIN_RAMP_STEPS
    kps = [x[1] for x in w]
    assert kps == sorted(kps) and kps[0] < 90 and kps[-1] == pytest.approx(120.0)
    assert w[-1][2] == pytest.approx(4.0)
    assert 0.08 <= dt < 1.0
    # only the changed joint was written
    assert {x[0] for x in cmd.arm.writes} == {idx}
    assert cmd.gains[idx] == (120.0, 4.0)


def test_refused_without_an_arm_and_while_limp(cmd):
    cmd.state = "LIMP"
    assert cmd.set_gains({"left_elbow_joint": {"kp": 10}}, ramp_s=0.0).startswith("refused")
    cmd.state = "HOLDING"
    cmd.arm = None
    assert cmd.set_gains({"left_elbow_joint": {"kp": 10}}, ramp_s=0.0).startswith("refused")


def test_hold_restores_the_commanders_gains_not_the_defaults(cmd):
    cmd.set_gains({"left_elbow_joint": {"kp": 120.0}}, ramp_s=0.0)
    cmd.arm.writes.clear()
    cmd.limp_arms()                       # kp -> 0 via set_arm_compliance
    assert cmd.state == "LIMP"
    cmd.hold()
    idx = ob.ARM_JOINT_INDEX["left_elbow_joint"]
    restored = {x[0]: x for x in cmd.arm.writes}
    assert restored[idx][1] == pytest.approx(120.0)
    assert cmd.arm.compliance[-1] == (0.0, 1.5)       # the limp call, not a restore-to-defaults


def test_save_and_reload(cmd, tmp_path):
    cmd.set_gains({"right_wrist_yaw_joint": {"kp": 60.0, "kd": 2.0}}, ramp_s=0.0)
    assert cmd.gains_save().startswith("gains saved")
    fresh = ob.Commander()
    fresh.arm = GainArm()
    with fresh.lock:
        fresh._init_gains(fresh.arm)
    idx = ob.ARM_JOINT_INDEX["right_wrist_yaw_joint"]
    assert fresh.gains[idx] == (60.0, 2.0)
    assert any(w[0] == idx and w[1] == 60.0 for w in fresh.arm.writes)
    r = fresh.gains_clear()
    assert not os.path.exists(ob.GAINS_PATH)
    assert fresh.gains[idx] == (40.0, 1.5)


def test_bounds_are_reported_per_joint():
    b = ob.Commander.gain_bounds()
    assert b["left_shoulder_pitch_joint"]["kp"] == [0.0, 150.0]
    assert b["right_wrist_pitch_joint"]["kp"] == [0.0, 80.0]
    assert b["left_elbow_joint"]["kd"] == [0.0, 5.0]


# --------------------------------------------------------------------------- #
# the per-frame ack
# --------------------------------------------------------------------------- #
def test_apply_reports_whether_it_commanded(cmd):
    cmd.measured_arm_q = [0.0] * ob.ARM_DIM
    cmd.state = "HOLDING"
    assert cmd.apply([0.0] * ob.ARM_DIM, [0.0] * 7, [0.0] * 7) is False
    cmd.last_rx_t = time.time()
    cmd.arm_on()
    assert cmd.apply([0.0] * ob.ARM_DIM, [0.0] * 7, [0.0] * 7) is True
    # a second frame inside the rate limit is read but not applied
    assert cmd.apply([0.0] * ob.ARM_DIM, [0.0] * 7, [0.0] * 7) is False


def test_ack_carries_seq_state_applied_and_measured(cmd):
    cmd.measured_arm_q = [0.25] * ob.ARM_DIM
    m = ob.ack_message(cmd, 812, True)
    assert m["ack"] == 812 and m["applied"] is True and m["state"] == "HOLDING"
    assert m["q"] == [0.25] * ob.ARM_DIM
    cmd.measured_arm_q = None
    assert ob.ack_message(cmd, 813, False)["q"] is None
    assert ob.idle_q_message(cmd)["state"] == "HOLDING"


def test_gains_are_refused_while_armed(cmd):
    """Stiffness is not adjustable on a moving arm.

    kp multiplies the standing position error, so changing it while the arm is
    tracking is a torque transient at the moment nobody is watching the panel.
    The ramp and the bounds make it survivable; that is the wrong bar when the
    jog panel's own flow already passes through HOLDING anyway.
    """
    cmd.state = "ARMED"
    before = list(cmd.gains)
    msg = cmd.set_gains({"left_elbow_joint": {"kp": 100.0, "kd": 2.0}})
    assert msg.startswith("refused") and "ARMED" in msg
    assert cmd.gains == before, "a refused change still altered the table"
    assert not cmd.arm.writes, "a refused change still reached the controller"


def test_gains_are_allowed_while_holding(cmd):
    """The state the jog panel actually uses."""
    cmd.state = "HOLDING"
    msg = cmd.set_gains({"left_elbow_joint": {"kp": 100.0, "kd": 2.0}}, ramp_s=0.0)
    assert not msg.startswith("refused"), msg
