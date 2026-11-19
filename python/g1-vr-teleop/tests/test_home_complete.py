"""Going home must take the hands too, and must know whether it arrived.

The operator: "make sure the go to home gets all the positions right and the
fingers as well".

Both halves were missing. go_home slewed the 14 arm joints and never touched
the Dex3 -- so it left the robot holding whatever the last attempt ended
with, which is a fist, because the last thing an attempt does is grip. And it
returned "at home" when the COMMAND reached the target, which says only that
it finished talking: a joint held off home by gravity or a table reported
success while sitting somewhere else, and every attempt after it started from
a pose nobody chose.
"""
import pathlib
import sys
import types

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))
sys.path.insert(0, str(ROOT / "onboard"))
import g1_onboard as ob                                          # noqa: E402

HOME = [0.1] * 14


class FakeArm:
    def __init__(self, measured=None):
        self.cmds = []
        self._q = np.zeros(14)
    def get_current_dual_arm_q(self): return self._q.copy()
    def ctrl_dual_arm(self, q, tau): self.cmds.append(np.asarray(q, float).copy())
    def speed_gradual_max(self, *a, **k): pass


class FakeHand:
    def __init__(self, **kw):
        self.grips = []
        self.relaxed = 0
    def ctrl_dual_hand(self, l, r): self.grips.append((list(l), list(r)))
    def relax(self): self.relaxed += 1


def _cmd(measured, hand=None):
    c = ob.Commander.__new__(ob.Commander)
    c.lock = __import__("threading").RLock()
    c.state = "HOLDING"
    c.arm = FakeArm()
    c.hand = hand
    c.home_q = list(HOME)
    c.homing = False
    c.measured_arm_q = measured
    c._last_cmd = None
    c._hold_pose = None
    return c


def test_the_hands_are_CLOSED_on_the_way_home():
    """Closed, at the operator's request.

    Home opened them first, on my reasoning that a fist is not a known
    starting pose. A closed hand is equally known, it is what they asked for,
    and it is the compact shape to move an arm across a table in. The pose is
    grip_q(side, 1.0), duplicated in g1_dex3 because the bridge does not run
    on the robot, and checked against it in test_dex3_order.py.
    """
    import g1_dex3
    hand = FakeHand()
    c = _cmd(list(HOME), hand)
    c.go_home()
    assert hand.grips, "the hands were never commanded"
    left, right = hand.grips[-1]
    assert left == g1_dex3.CLOSED_LEFT and right == g1_dex3.CLOSED_RIGHT


def test_a_hand_that_errors_does_not_strand_the_arm():
    class Broken(FakeHand):
        def ctrl_dual_hand(self, l, r): raise RuntimeError("dex3 down")
    c = _cmd(list(HOME), Broken())
    assert "home" in c.go_home()
    assert c.arm.cmds, "the arm still moved"


def test_no_hand_at_all_is_fine():
    c = _cmd(list(HOME), None)
    assert "home" in c.go_home()


# ---- arrival ----------------------------------------------------------------

def test_arriving_is_reported_as_arriving():
    c = _cmd(list(HOME), FakeHand())
    reply = c.go_home()
    assert reply.startswith("at home"), reply


def test_a_joint_held_away_from_home_is_NOT_reported_as_home():
    """The failure this exists for: something holds the arm, the command
    reaches the target, and the old code said "at home" regardless."""
    stuck = list(HOME)
    stuck[3] = HOME[3] + 0.6                    # ~34 deg off
    c = _cmd(stuck, FakeHand())
    reply = c.go_home()
    assert "NOT there" in reply, reply
    assert "34" in reply or "35" in reply, reply
    assert ob.ARM_JOINT_NAMES[3] in reply, "it must name the joint"


def test_ordinary_droop_still_counts_as_home():
    """A held arm sags 2-3 deg; calling that "not home" would cry wolf."""
    sag = [v + 0.04 for v in HOME]              # ~2.3 deg
    assert _cmd(sag, FakeHand()).go_home().startswith("at home")


def test_without_measured_joints_it_says_so_rather_than_claiming_success():
    c = _cmd(None, FakeHand())
    reply = c.go_home()
    assert "commanded" in reply and "no measured" in reply


def test_the_existing_refusals_are_unchanged():
    c = _cmd(list(HOME), FakeHand())
    c.state = "ARMED"
    assert c.go_home().startswith("refused")
    c.state, c.arm = "HOLDING", None
    assert c.go_home().startswith("refused")
    c.arm, c.home_q = FakeArm(), None
    assert "no home captured" in c.go_home()
