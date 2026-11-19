"""Joint limits: the hands had none, and the arms used datasheet numbers.

sanitize_hand checked finiteness and passed finger targets to the motors
unbounded -- the only path in the commander that validated nothing. And the
Dex3 limits are MIRRORED between hands, so one shared table would drive one
hand's fingers into their stops.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "onboard"))
import g1_onboard as ob  # noqa: E402


# --------------------------------------------------------------------------- #
# hands
# --------------------------------------------------------------------------- #
def test_finger_targets_are_clamped_at_all():
    out = ob.sanitize_hand([99.0] * 7, "left")
    assert all(abs(v) < 10 for v in out), "unbounded finger target reached a motor"


def test_left_and_right_limits_are_not_the_same():
    """Mirrored joints: a shared table would command the wrong direction."""
    assert ob.DEX3_LIMITS["left"] != ob.DEX3_LIMITS["right"]
    # middle_0 is the clearest case: left is negative-going, right positive.
    assert ob.DEX3_LIMITS["left"][3] == (-1.57079632, 0.0)
    assert ob.DEX3_LIMITS["right"][3] == (0.0, 1.57079632)


@pytest.mark.parametrize("side", ["left", "right"])
def test_every_finger_target_lands_inside_its_own_limit(side):
    for v in (-99.0, -1.0, 0.0, 1.0, 99.0):
        out = ob.sanitize_hand([v] * 7, side)
        for i, got in enumerate(out):
            lo, hi = ob.DEX3_LIMITS[side][i]
            assert lo - 1e-9 <= got <= hi + 1e-9, f"{side}[{i}] = {got} outside {lo}..{hi}"


def test_a_bad_side_is_refused():
    with pytest.raises(ValueError):
        ob.sanitize_hand([0.0] * 7, "middle")


def test_non_finite_finger_values_are_still_refused():
    with pytest.raises(ValueError):
        ob.sanitize_hand([float("nan")] + [0.0] * 6, "left")


# --------------------------------------------------------------------------- #
# arm calibration
# --------------------------------------------------------------------------- #
@pytest.fixture
def cmd(tmp_path, monkeypatch):
    monkeypatch.setattr(ob, "CAL_PATH", str(tmp_path / "cal.json"))
    monkeypatch.setattr(ob, "ARM_LIMITS_CAL", None)
    c = ob.Commander()
    c.measured_arm_q = [0.0] * 14
    return c


def _walk(c, lo, hi):
    """Pretend the operator moved every joint between lo and hi."""
    for q in ([lo] * 14, [hi] * 14):
        c.measured_arm_q = q
        with c.lock:
            for i, v in enumerate(q):
                c.cal["lo"][i] = min(c.cal["lo"][i], v)
                c.cal["hi"][i] = max(c.cal["hi"][i], v)


def test_calibration_narrows_the_envelope(cmd):
    assert "calibrating" in cmd.calibrate_start()
    _walk(cmd, -0.8, 0.9)
    msg = cmd.calibrate_stop()
    assert "calibrated 14/14" in msg
    lim = ob.effective_arm_limits()
    assert lim[0][0] == pytest.approx(-0.8 + ob.CAL_MARGIN_RAD)
    assert lim[0][1] == pytest.approx(0.9 - ob.CAL_MARGIN_RAD)
    # and a target beyond it is now clamped to the calibrated value
    assert ob.sanitize_arm([5.0] * 14)[0] == pytest.approx(0.9 - ob.CAL_MARGIN_RAD)


def test_calibration_can_never_widen_past_mechanical(cmd):
    """A sloppy calibration must not unlock more range than the joint has."""
    cmd.calibrate_start()
    _walk(cmd, -99.0, 99.0)
    cmd.calibrate_stop()
    for got, mech in zip(ob.effective_arm_limits(), ob.ARM_LIMITS):
        assert got[0] >= mech[0] - 1e-9
        assert got[1] <= mech[1] + 1e-9


def test_an_unexercised_joint_keeps_its_mechanical_limits(cmd):
    """Clamping the wrists to 2 degrees because nobody moved them would
    freeze teleop and look exactly like a broken link."""
    cmd.calibrate_start()
    cmd.measured_arm_q = [0.0] * 14
    with cmd.lock:                     # only joint 0 is moved
        cmd.cal["lo"][0], cmd.cal["hi"][0] = -0.6, 0.6
    msg = cmd.calibrate_stop()
    assert "were not exercised" in msg
    lim = ob.effective_arm_limits()
    assert lim[1] == ob.ARM_LIMITS[1], "an unmoved joint was locked down"
    assert lim[0] != ob.ARM_LIMITS[0], "the moved joint was not narrowed"


def test_calibration_is_refused_while_not_in_standby(cmd):
    cmd.state = "ARMED"
    assert cmd.calibrate_start().startswith("refused")


def test_calibration_survives_a_restart(cmd):
    cmd.calibrate_start()
    _walk(cmd, -0.5, 0.5)
    cmd.calibrate_stop()
    assert ob._load_cal() is not None
    assert ob._load_cal()[0][1] == pytest.approx(0.5 - ob.CAL_MARGIN_RAD)


def test_clearing_returns_to_mechanical(cmd):
    cmd.calibrate_start()
    _walk(cmd, -0.5, 0.5)
    cmd.calibrate_stop()
    cmd.calibrate_clear()
    assert ob.effective_arm_limits() == ob.ARM_LIMITS


# --------------------------------------------------------------------------- #
# limp: pose the arms by hand without limping the legs
# --------------------------------------------------------------------------- #
class FakeArmGains:
    def __init__(self):
        self.kp_low, self.kd_low = 80.0, 3.0
        self.kp_wrist, self.kd_wrist = 40.0, 1.5
        self.compliance = []
        self.sent = []

    def set_arm_compliance(self, kp=None, kd=None):
        self.compliance.append((kp, kd))

    def get_current_dual_arm_q(self):
        return [0.1] * 14

    def ctrl_dual_arm(self, q, tau):
        self.sent.append(list(q))


def test_limp_softens_the_arms(cmd):
    cmd.arm = FakeArmGains()
    cmd.state = "HOLDING"
    reply = cmd.limp_arms()
    assert reply.startswith("LIMP")
    assert cmd.state == "LIMP"
    assert cmd.arm.compliance == [(0.0, 1.5)], "arm gains were not zeroed"


def test_limp_is_refused_while_armed(cmd):
    cmd.arm = FakeArmGains()
    cmd.state = "ARMED"
    assert cmd.limp_arms().startswith("refused")
    assert cmd.state == "ARMED"


def test_hold_stiffens_before_freezing(cmd):
    """Freezing at the measured pose is meaningless with no position gain."""
    cmd.arm = FakeArmGains()
    cmd.hand = None
    cmd.state = "LIMP"
    cmd.hold()
    assert cmd.arm.compliance[-1] == (None, None), "never restored stiffness"
    assert cmd.arm.sent, "did not command a pose to hold"
    assert cmd.state == "HOLDING"


def test_calibration_runs_from_limp(cmd):
    cmd.state = "LIMP"
    cmd.measured_arm_q = [0.0] * 14
    assert "calibrating" in cmd.calibrate_start()


def test_calibration_still_refused_while_armed(cmd):
    cmd.state = "ARMED"
    cmd.measured_arm_q = [0.0] * 14
    assert cmd.calibrate_start().startswith("refused")
