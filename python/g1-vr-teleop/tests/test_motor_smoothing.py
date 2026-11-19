"""Exercise the real publisher with a fake DDS transport, never hardware."""
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from tests.test_waist_yaw import fake_sdk  # noqa: F401


def publisher_trace(monkeypatch, targets, dt=.01):
    import g1_arm
    ctl = g1_arm.G1_29_ArmController.__new__(g1_arm.G1_29_ArmController)
    ctl.ctrl_lock = threading.Lock()
    ctl.motion_mode = ctl.simulation_mode = False
    ctl.control_dt = dt
    ctl.arm_velocity_limit = 30.0
    ctl.q_target = np.zeros(14)
    ctl.tauff_target = np.zeros(14)
    ctl.msg = SimpleNamespace(motor_cmd=[SimpleNamespace(q=0., dq=0., tau=0.) for _ in range(35)])
    ctl.crc = SimpleNamespace(Crc=lambda msg: 0)
    ctl._running = True
    ctl.loop_worst_ms = ctl._loop_n = 0
    ctl._loop_t0 = None
    ctl.record_reading = None
    clock = [10.0]
    trace = []
    measured = np.zeros(14)
    ctl.get_current_dual_arm_q = lambda: measured.copy()
    ctl.get_current_waist_q = lambda: 0.0
    ctl._advance_waist_velocity = lambda **kw: None

    def write(msg):
        q = np.array([msg.motor_cmd[i].q for i in range(15, 29)])
        trace.append(q.copy())
        measured[:] = q  # perfect plant: isolates command discontinuities
        if len(trace) == len(targets):
            ctl._running = False
        else:
            ctl.ctrl_dual_arm(np.full(14, targets[len(trace)]), np.zeros(14))

    ctl.lowcmd_publisher = SimpleNamespace(Write=write)
    # Replace this module's clock, not Python's process-global time module:
    # other controller tests have live daemon subscribers that must still sleep.
    monkeypatch.setattr(g1_arm, "time", SimpleNamespace(
        time=lambda: clock[0], monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + dt)))
    ctl._ctrl_motor_state()
    return ctl, np.array(trace)


def test_motor_writer_bounds_acceleration_through_steps_and_reversals(fake_sdk, monkeypatch):
    ctl, q = publisher_trace(monkeypatch, [0.] * 10 + [1.] * 150 + [-1.] * 150 + [0.] * 150)
    velocity = np.diff(q, axis=0) / .01
    acceleration = np.diff(velocity, axis=0) / .01
    assert ctl.arm_reference.max_velocity == 35.0
    assert np.max(np.abs(velocity)) <= 35.0 + 1e-8
    assert np.max(np.abs(acceleration)) <= 16.0 + 1e-8
    assert np.max(np.abs(q[-1])) < .03


def test_faster_reference_bandwidth_changes_response_without_more_acceleration():
    from arm_reference import ArmReference

    def time_to_95_percent(frequency):
        reference = ArmReference([0.0])
        reference.frequency = frequency
        measured = reference.q.copy()
        trace = []
        for tick in range(1, 301):
            measured = reference.advance([2.0], measured, .01)
            trace.append(float(reference.q[0]))
            if trace[-1] >= 1.9:
                velocity = np.diff([0.0] + trace) / .01
                acceleration = np.diff(velocity) / .01
                return tick / 100.0, np.max(np.abs(acceleration))
        raise AssertionError("reference did not reach the target")

    old_time, _ = time_to_95_percent(20.0)
    new_time, new_acceleration = time_to_95_percent(ArmReference.frequency)
    assert old_time == pytest.approx(1.15, abs=.02)
    assert new_time == pytest.approx(.65, abs=.02)
    assert new_acceleration <= ArmReference.max_acceleration + 1e-8


def test_publisher_records_the_actual_target_after_smoothing(fake_sdk, monkeypatch):
    ctl, q = publisher_trace(monkeypatch, [0.] * 3 + [1.] * 20)
    report = ctl.arm_reference_report()
    assert report["q"] == pytest.approx(q[-1])
    assert report["requested_q"] == pytest.approx(np.ones(14))
    assert report["max_acceleration_rad_s2"] == 16.0


def test_deployed_profile_overrides_old_fit_without_overwriting_saved_gains(tmp_path, monkeypatch):
    import json
    import g1_onboard as ob
    from tests.test_onboard_gains import GainArm
    path = tmp_path / "gains.json"
    saved = json.dumps({"gains": [[80, 3] if "wrist" in n else [150, 5]
                                  for n in ob.ARM_JOINT_ORDER]})
    path.write_text(saved)
    monkeypatch.setattr(ob, "GAINS_PATH", str(path))
    monkeypatch.setenv("G1_ARM_GAIN_PROFILE", "smooth_v1")
    cmd = ob.Commander()
    cmd.arm = GainArm()
    cmd._init_gains(cmd.arm)
    assert cmd.gains_report()["right_shoulder_pitch_joint"] == {"kp": 80, "kd": 5}
    assert cmd.gains_report()["right_wrist_pitch_joint"] == {"kp": 40, "kd": 3}
    assert path.read_text() == saved
    monkeypatch.setenv("G1_ARM_GAIN_PROFILE", "saved")
    cmd._init_gains(cmd.arm)
    assert cmd.gains_report()["right_shoulder_pitch_joint"] == {"kp": 150, "kd": 5}


def test_hold_discards_reference_momentum(fake_sdk, monkeypatch):
    ctl, q = publisher_trace(monkeypatch, [0.] * 3 + [1.] * 20)
    assert np.max(np.abs(ctl.arm_reference.velocity)) > .1
    ctl.hold_arm_measured()
    assert ctl.arm_reference.q == pytest.approx(q[-1])
    assert ctl.arm_reference.velocity == pytest.approx(np.zeros(14))
    assert ctl.q_target == pytest.approx(q[-1])


def test_packet_bursts_only_update_destination(fake_sdk, monkeypatch):
    ctl, _ = publisher_trace(monkeypatch, [0.] * 3)
    before = ctl.arm_reference.q.copy()
    target = np.ones(14)
    for _ in range(100):
        ctl.ctrl_dual_arm(target, np.zeros(14))
    target[:] = 99  # caller mutation must not bypass bounds
    assert ctl.arm_reference.q == pytest.approx(before)
    assert ctl.q_target == pytest.approx(np.ones(14))


def test_calibrated_limits_and_stalled_joint_leash_take_priority():
    from arm_reference import ArmReference
    reference = ArmReference([0., 0.])
    limits = np.array([[-.1, .1], [-1., 1.]])
    for _ in range(1000):
        q = reference.advance([2., 2.], [0., 0.], .01, limits)
        assert q[0] <= .1
        assert q[1] <= np.deg2rad(10) + 1e-9
    # A sensor/physical jump must not let the command remain far from reality.
    q = reference.advance([2., 2.], [-.05, -.8], .01, limits)
    assert np.max(np.abs(q - [-.05, -.8])) <= np.deg2rad(10) + 1e-9
    assert reference.guard_hits > 0


def test_long_tick_gap_does_not_allow_a_catchup_jump():
    from arm_reference import ArmReference
    reference = ArmReference([0.])
    q = reference.advance([1.], [0.], 10.)
    assert q[0] <= 16 * .02**2 + 1e-9


