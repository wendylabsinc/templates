"""A finger stalled against an object must not sit at full position error.

The knuckles (`*_0`) carry the whole finger. Closing on a can they stop on the
object while the command stays at the range end, so the motor holds kp x the
full gap for the whole grip -- stall current, heat. In the handfix session the
knuckles fell from 98 % follow to 12-17 % and never recovered; the outer joints
of the same fingers, which curl freely, never degraded. The cause is not
confirmed (thermal derating and a latched fault both fit); this is the
mitigation for the mechanism both share, and it only shrinks the holding
error, never the operator's intent: the instant the command comes back toward
the finger, it passes straight through.
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "onboard"))
sys.path.insert(0, os.path.join(ROOT, "teleop", "bridge"))

import dex3_stall as ds  # noqa: E402
import retarget_g1 as rg  # noqa: E402

ORDER = ["thumb_0", "thumb_1", "thumb_2", "middle_0", "middle_1", "index_0", "index_1"]
MID0 = ORDER.index("middle_0")


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _guard(**kw):
    clk = Clock()
    return ds.StallBackoff(clock=clk, **kw), clk


def test_a_moving_finger_is_never_backed_off():
    g, clk = _guard()
    cmd = [0.0] * 7
    cmd[MID0] = 1.5
    for i in range(30):                       # measured tracking the command
        meas = [0.0] * 7
        meas[MID0] = 0.05 * i
        out, names = g.apply("right", cmd, meas)
        clk.t += 0.05
        assert names == [] and out == cmd


def test_a_stalled_finger_is_backed_off_after_the_hold_time():
    g, clk = _guard(stall_s=0.5, gap_rad=0.3, margin_rad=0.35)
    cmd = [0.0] * 7
    cmd[MID0] = 1.5
    meas = [0.0] * 7
    meas[MID0] = 0.4                          # stopped on the can, 1.1 rad short
    out, names = g.apply("right", cmd, meas)
    assert names == [], "the first frame cannot know it is a stall"
    clk.t += 0.3
    out, names = g.apply("right", cmd, meas)
    assert names == [], "0.3 s still is not yet a stall"
    clk.t += 0.3
    out, names = g.apply("right", cmd, meas)
    assert names == ["middle_0"]
    assert out[MID0] == pytest.approx(0.4 + 0.35)
    assert out[:MID0] == cmd[:MID0] and out[MID0 + 1:] == cmd[MID0 + 1:], "only the stalled joint changes"


def test_the_sign_follows_the_command_on_both_hands():
    """Left curl joints run negative, right ones positive (DEX3_RANGE)."""
    g, clk = _guard()
    # The margin comes from the guard, not a literal: it is the grip force and
    # it moved (0.35 -> 0.15) when two thumbs cooked at 86 C. A test that
    # spells it out again just breaks when the robot teaches us the number.
    margin = g.margin_rad
    for side, sign in (("left", -1.0), ("right", 1.0)):
        cmd = [0.0] * 7
        cmd[MID0] = sign * 1.5
        meas = [0.0] * 7
        meas[MID0] = sign * 0.4
        g.apply(side, cmd, meas)
        clk.t += 1.0
        out, names = g.apply(side, cmd, meas)
        assert names == ["middle_0"]
        assert out[MID0] == pytest.approx(sign * (0.4 + margin))


def test_the_margin_never_overshoots_the_command():
    g, clk = _guard(gap_rad=0.1, margin_rad=0.35)
    cmd = [0.0] * 7
    cmd[MID0] = 0.6
    meas = [0.0] * 7
    meas[MID0] = 0.4                          # gap 0.2 > 0.1, but 0.4 + 0.35 > 0.6
    g.apply("right", cmd, meas)
    clk.t += 1.0
    out, names = g.apply("right", cmd, meas)
    assert names == ["middle_0"] and out[MID0] == pytest.approx(0.6)


def test_recovery_is_immediate_when_the_command_comes_back():
    g, clk = _guard()
    cmd = [0.0] * 7
    cmd[MID0] = 1.5
    meas = [0.0] * 7
    meas[MID0] = 0.4
    g.apply("right", cmd, meas)
    clk.t += 1.0
    out, names = g.apply("right", cmd, meas)
    assert names == ["middle_0"]
    opening = list(cmd)
    opening[MID0] = 0.0                       # operator lets go
    out, names = g.apply("right", opening, meas)
    assert names == [] and out == opening, "an opening command must pass straight through"


def test_the_stall_is_forgotten_when_the_finger_moves():
    g, clk = _guard()
    cmd = [0.0] * 7
    cmd[MID0] = 1.5
    meas = [0.0] * 7
    meas[MID0] = 0.4
    g.apply("right", cmd, meas)
    clk.t += 1.0
    assert g.apply("right", cmd, meas)[1] == ["middle_0"]
    meas[MID0] = 0.9                          # the can gave way; the finger moved
    out, names = g.apply("right", cmd, meas)
    assert names == [] and out == cmd
    clk.t += 0.3
    assert g.apply("right", cmd, meas)[1] == [], "the hold timer restarted at the move"


def test_no_measurement_means_no_backoff():
    g, clk = _guard()
    cmd = [0.0] * 7
    cmd[MID0] = 1.5
    for _ in range(3):
        clk.t += 1.0
        out, names = g.apply("right", cmd, None)
        assert names == [] and out == cmd


def test_disabled_by_env_passes_everything_through():
    g = ds.StallBackoff.from_env({"G1_DEX3_STALL_BACKOFF": "0"}, clock=Clock())
    assert g.enabled is False
    cmd = [0.0] * 7
    cmd[MID0] = 1.5
    meas = [0.0] * 7
    meas[MID0] = 0.4
    g.apply("right", cmd, meas)
    g.clock.t += 5.0
    out, names = g.apply("right", cmd, meas)
    assert names == [] and out == cmd


def test_env_thresholds_are_read_and_bounded():
    g = ds.StallBackoff.from_env({"G1_DEX3_STALL_S": "0.8", "G1_DEX3_STALL_GAP_RAD": "0.2",
                                  "G1_DEX3_HOLD_MARGIN_RAD": "0.25"})
    assert (g.stall_s, g.gap_rad, g.margin_rad) == (0.8, 0.2, 0.25)
    with pytest.raises(ValueError):
        ds.StallBackoff.from_env({"G1_DEX3_HOLD_MARGIN_RAD": "nan"})
    with pytest.raises(ValueError):
        ds.StallBackoff.from_env({"G1_DEX3_STALL_S": "-1"})


# ---- the closed pose the onboard falls back to, and the payload it accepts ----

def test_the_default_closed_pose_matches_grip_q():
    for side, const in (("left", ds.CLOSED_LEFT), ("right", ds.CLOSED_RIGHT)):
        want = rg.grip_q(side, 1.0)
        assert [round(a, 6) for a in const] == [round(b, 6) for b in want]


def test_the_range_table_matches_the_bridge():
    for side, table in (("left", ds.RANGE_LEFT), ("right", ds.RANGE_RIGHT)):
        for i, j in enumerate(ORDER):
            assert table[i] == pytest.approx(rg.DEX3_RANGE[f"{side}_hand_{j}_joint"])


def test_a_valid_hand_pose_payload_is_accepted_and_clamped():
    left = list(ds.CLOSED_LEFT)
    right = list(ds.CLOSED_RIGHT)
    right[0] = 9.0                            # thumb_0 way past its +1.047 range
    got = ds.sanitize_hand_pose({"left": left, "right": right})
    assert got is not None
    assert got[0] == pytest.approx(left)
    assert got[1][0] == pytest.approx(ds.RANGE_RIGHT[0][1])
    assert got[1][1:] == pytest.approx(right[1:])


@pytest.mark.parametrize("bad", [
    None, "x", {"left": [0.0] * 7}, {"left": [0.0] * 6, "right": [0.0] * 7},
    {"left": [float("nan")] + [0.0] * 6, "right": [0.0] * 7},
    {"left": ["a"] * 7, "right": [0.0] * 7},
])
def test_a_malformed_hand_pose_payload_is_refused(bad):
    assert ds.sanitize_hand_pose(bad) is None


# ---- a faulted motor is not a stalled one -----------------------------------

def test_a_disabled_motor_is_skipped_rather_than_backed_off():
    """Measured on the robot: the right thumb_1 reported mode 0 and
    motorstate 512 on every one of 2675 frames, cold, and the back-off held
    it on 2629 of them. A motor that is not listening cannot be relieved by
    commanding it less, and "backed off" is the wrong word for it -- "faulted"
    sends somebody to the hand instead of to the code.
    """
    t = [100.0]
    b = ds.StallBackoff(stall_s=0.5, gap_rad=0.3, margin_rad=0.35, clock=lambda: t[0])
    cmd = [0.0, -0.920, 0.0, 1.571, 1.745, 1.571, 1.745]
    meas = [0.0, 0.000, 0.0, 0.300, 1.700, 1.560, 1.700]     # thumb_1, middle_0 stuck
    mode = [1, 0, 1, 1, 1, 1, 1]                             # thumb_1 DISABLED
    for step in (0.0, 0.3, 1.0, 2.0):
        t[0] = 100.0 + step
        out, names = b.apply("right", cmd, meas, mode)
    assert "thumb_1" not in names, "a faulted motor was reported as backed off"
    assert out[1] == cmd[1], "and its command was altered"
    assert "middle_0" in names, "a genuinely stalled joint must still back off"


def test_without_a_mode_vector_it_behaves_exactly_as_before():
    """An older onboard sends none, and losing the back-off entirely would be
    worse than treating one dead motor as stalled."""
    t = [100.0]
    b = ds.StallBackoff(stall_s=0.5, gap_rad=0.3, margin_rad=0.35, clock=lambda: t[0])
    cmd = [0.0, 0.0, 0.0, 1.571, 1.745, 1.571, 1.745]
    meas = [0.0, 0.0, 0.0, 0.300, 1.700, 1.560, 1.700]
    for step in (0.0, 1.0):
        t[0] = 100.0 + step
        out, names = b.apply("right", cmd, meas)
    assert "middle_0" in names
