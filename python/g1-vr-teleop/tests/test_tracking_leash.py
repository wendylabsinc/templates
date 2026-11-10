"""The command must not walk away from where the arm actually is.

ARM_SLEW_RAD_S bounds how fast the command CHANGES. It says nothing about how
far the command drifts from reality, and the two are different failures: a
blocked, stalled or overloaded joint satisfies the slew check forever while the
gap grows without bound. _slew chains off self._last_cmd and touches the
measurement only to SEED, at arming and after a freeze.

`measured_arm_q` had been read from lowstate and plumbed into /status and home
capture since yesterday, and was never once compared to the command.
"""
import math
import os
import sys
import threading

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "onboard"))
import g1_onboard as ob  # noqa: E402


class FakeArm:
    def __init__(self, q):
        self.q = np.asarray(q, float)
        self.sent = []

    def get_current_dual_arm_q(self):
        return self.q.copy()

    def ctrl_dual_arm(self, q, tau):
        self.sent.append(np.asarray(q, float).copy())


def _cmd(measured):
    c = ob.Commander.__new__(ob.Commander)
    c.motion_limits = {"max_velocity_rad_s": 3.5, "max_acceleration_rad_s2": 16.}
    c.lock = threading.RLock()
    c.state = "ARMED"
    c.hand = None
    c.arm = FakeArm(measured)
    c.deadman_bit = None
    c.remote_t = 0.0
    c.discover = False
    c._release_seen = True
    c.deadman_held = False
    c._last_cmd = None
    c._last_cmd_t = 0.0
    c.last_apply_t = 0.0
    c.last_frame_t = 0.0
    c.measured_arm_q = list(measured)
    c.n_applied = c.n_rejected = c.n_watchdog = c.n_armed = 0
    c.n_leashed = 0
    c._leash_log_t = 0.0
    c._track_err = []
    c.arm_cmd_ms = c.hand_cmd_ms = None
    c.last_freeze_why = ""
    return c


def test_a_stalled_joint_stops_the_command_ten_degrees_away():
    """THE case. The arm is frozen; the command must stop, not run to target."""
    measured = np.zeros(14)
    c = _cmd(measured)
    for _ in range(2000):                 # far more than enough to reach 1.5 rad
        c.last_apply_t = 0.0
        c.apply([1.5] * 14, [0.0] * 7, [0.0] * 7)
    final = c.arm.sent[-1]
    assert np.all(final <= ob.MAX_TRACKING_ERROR_RAD + 1e-9), (
        f"command ran to {final.max():.3f} rad against a frozen arm; "
        f"limit is {ob.MAX_TRACKING_ERROR_RAD:.3f}")
    assert final.max() == pytest.approx(ob.MAX_TRACKING_ERROR_RAD, abs=1e-6)
    assert c.n_leashed > 0, "clamping was never recorded"


def test_the_slew_limit_alone_does_not_catch_this():
    """Characterise why the leash is needed: every step is slew-legal."""
    measured = np.zeros(14)
    c = _cmd(measured)
    steps = []
    for _ in range(50):
        c.last_apply_t = 0.0
        before = None if not c.arm.sent else c.arm.sent[-1].copy()
        c.apply([1.5] * 14, [0.0] * 7, [0.0] * 7)
        if before is not None:
            steps.append(float(np.max(np.abs(c.arm.sent[-1] - before))))
    assert steps, "no frames applied"
    assert max(steps) <= ob.ARM_SLEW_RAD_S * 0.1 + 1e-9, \
        "the slew limit was satisfied throughout — it cannot see divergence"


def test_an_arm_that_follows_never_trips_the_leash():
    """Normal free-space teleop must not clamp. If it does, the threshold or
    the gains are wrong."""
    c = _cmd(np.zeros(14))
    target = np.full(14, 0.6)
    for _ in range(400):
        c.last_apply_t = 0.0
        c.apply(list(target), [0.0] * 7, [0.0] * 7)
        # the arm follows the command closely, as a healthy one does
        c.measured_arm_q = list(c.arm.sent[-1] - 0.01)
        c.arm.q = np.asarray(c.measured_arm_q, float)
    assert c.n_leashed == 0, "healthy tracking tripped the leash"
    assert np.allclose(c.arm.sent[-1], target, atol=0.02)


def test_the_leash_corrects_the_slew_chain():
    """If _last_cmd kept the unclamped value, _slew would keep walking away and
    the clamp would fight a runaway every frame."""
    c = _cmd(np.zeros(14))
    for _ in range(200):
        c.last_apply_t = 0.0
        c.apply([1.5] * 14, [0.0] * 7, [0.0] * 7)
    assert np.all(np.abs(c._last_cmd) <= ob.MAX_TRACKING_ERROR_RAD + 1e-9), (
        "_last_cmd kept the pre-clamp value; the chain is running away")


def test_tracking_error_is_reported_even_when_nothing_is_clamped():
    """Verification-plan measurement #2, which the leash gives us for free."""
    c = _cmd(np.zeros(14))
    for _ in range(30):
        c.last_apply_t = 0.0
        c.apply([0.05] * 14, [0.0] * 7, [0.0] * 7)
    r = c.tracking_report()
    assert r["samples"] > 0
    assert r["clamped_frames"] == 0
    assert r["limit_deg"] == pytest.approx(10.0, abs=0.1)
    assert 0.0 <= r["p95_deg"] <= 10.0


def test_no_measurement_yet_does_not_block_commands():
    """Before the first lowstate, the leash must not gate motion."""
    c = _cmd(np.zeros(14))
    c.measured_arm_q = None
    c.apply([0.2] * 14, [0.0] * 7, [0.0] * 7)
    assert c.arm.sent, "commands were blocked before the first measurement"


def test_leash_uses_fresh_motor_feedback_at_higher_speed():
    """Decimated dashboard feedback must not falsely leash a following arm."""
    c = _cmd(np.full(14, .1))
    c.measured_arm_q = [0.] * 14  # dashboard is ~33 ms behind
    q = c._leash(np.full(14, .2), 10.)
    assert q == pytest.approx(np.full(14, .2))
    assert c.n_leashed == 0
    # The opposite case still catches a stalled motor, using its current pose.
    c.arm.q[:] = 0.
    c.measured_arm_q = [.1] * 14
    q = c._leash(np.full(14, .2), 11.)
    assert q == pytest.approx(np.full(14, ob.MAX_TRACKING_ERROR_RAD))
    assert c.n_leashed == 1
