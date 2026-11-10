"""Keep the knuckles out of their cutout: there is no way back from one.

Measured 2026-09-15, left hand, one 49 s session, by thirds:

    joint      temp 1st   2nd    3rd   mode  motorstate   follow
    middle_0      39 C   70 C   90 C      0         512   100 -> 63 -> 40 %
    index_0       39 C   69 C   90 C      0         512    98 -> 68 -> 29 %
    index_1       35 C   48 C   61 C      1           0    99 % throughout

The joints that reached 90 C are exactly the ones the hand disabled. A trip
cannot be undone in software: `_RIS_Mode` carries a 3-bit status, 0x01 is
already "enable", the onboard writes it at 100 Hz, and a tripped motor keeps
answering `mode 0`. So the only available move is not to trip.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))
sys.path.insert(0, str(ROOT / "onboard"))
from dex3_thermal import ORDER, ThermalGuard                    # noqa: E402

MID = ORDER.index("middle_0")


def _t(c):
    """All seven joints at one temperature."""
    return [c] * len(ORDER)


def test_a_cold_hand_grips_at_full_strength():
    """An idle hand measured 34-40 C. Weakening that would be a regression in
    every grasp to fix a fault that has not started."""
    g = ThermalGuard()
    assert g.scale("left", _t(40)) == [1.0] * 7


def test_it_backs_off_before_the_measured_midpoint():
    """The climb was 39 -> 70 -> 90 C in 49 s. Starting at 70 leaves 20 C of
    headroom and no time to use it."""
    g = ThermalGuard()
    assert g.scale("left", _t(70))[MID] < 0.6, "still near full gain at 70 C"


def test_it_is_at_its_floor_by_the_trip_temperature():
    g = ThermalGuard()
    assert g.scale("left", _t(90))[MID] == pytest.approx(0.25)


def test_the_floor_is_never_zero():
    """A finger with no stiffness drops what it is holding. Dropping a can to
    save a motor is not the trade anyone wants mid-grasp."""
    g = ThermalGuard()
    assert min(g.scale("left", _t(200))) > 0.0


def test_it_is_monotonic_in_temperature():
    """A hotter joint may never be given more gain than a cooler one."""
    g = ThermalGuard()
    prev = 2.0
    for c in range(30, 110, 2):
        s = g.scale("left", _t(c))[MID]
        assert s <= prev + 1e-9, f"gain rose at {c} C"
        prev = s


def test_only_the_hot_joint_is_held_back():
    """The outer joints of the same fingers ran 61-78 C and followed at 99 %;
    weakening them would cost a working grip for nothing."""
    g = ThermalGuard()
    temps = [38, 39, 90, 90, 50, 90, 45]
    s = g.scale("left", temps)
    assert s[ORDER.index("middle_1")] == 1.0
    assert s[ORDER.index("index_1")] == 1.0
    assert s[ORDER.index("middle_0")] < 0.5


def test_a_hand_that_reports_nothing_grips_normally():
    """A missing reading is not a reason to weaken every grip on the robot."""
    g = ThermalGuard()
    assert g.scale("left", None) == [1.0] * 7
    assert g.scale("left", []) == [1.0] * 7
    assert g.scale("left", [None] * 7) == [1.0] * 7


def test_a_single_missing_sensor_does_not_affect_its_neighbours():
    g = ThermalGuard()
    temps = [38, None, 90, 39, 40, 41, 42]
    s = g.scale("left", temps)
    assert s[1] == 1.0 and s[2] < 0.5 and s[3] == 1.0


def test_it_can_be_turned_off_entirely():
    g = ThermalGuard.from_env({"G1_DEX3_THERMAL": "0"})
    assert g.scale("left", _t(120)) == [1.0] * 7


def test_the_thresholds_are_tunable():
    g = ThermalGuard.from_env({"G1_DEX3_WARM_C": "40", "G1_DEX3_HOT_C": "60"})
    assert g.scale("left", _t(50))[MID] < 1.0, "40 C threshold not applied"
    assert g.scale("left", _t(60))[MID] == pytest.approx(g.min_scale)


def test_nonsense_thresholds_fall_back_rather_than_disabling_the_guard():
    g = ThermalGuard.from_env({"G1_DEX3_WARM_C": "banana", "G1_DEX3_HOT_C": "-5"})
    assert g.scale("left", _t(90))[MID] < 1.0


def test_hot_below_warm_cannot_invert_the_curve():
    """A typo must not make a hotter joint stronger."""
    g = ThermalGuard(warm_c=80.0, hot_c=40.0)
    assert g.scale("left", _t(100))[MID] <= g.scale("left", _t(50))[MID]


def test_status_names_only_the_joints_being_held_back():
    g = ThermalGuard()
    g.scale("left", [38, 39, 90, 39, 40, 41, 42])
    st = g.status()
    assert list(st["left"]) == ["thumb_2"]


def test_the_sender_scales_the_gains_and_leaves_position_alone():
    """Lowering kp lowers holding torque, which is what makes the heat. Moving
    the finger somewhere else would be a grip nobody asked for."""
    src = (ROOT / "onboard" / "g1_dex3.py").read_text()
    body = src.split("def ctrl_dual_hand")[1].split("\n    def ")[0]
    assert "self.kp * ls[i]" in body and "self.kp * rs[i]" in body
    assert ".q = float(left_q7[i])" in body, "position must be commanded as given"
    assert "* ls[i]" not in body.split(".q = float(left_q7[i])")[1].split("\n")[0]
