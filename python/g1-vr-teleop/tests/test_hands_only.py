"""Hands-only mode: the arms must be physically unable to move.

The point of this mode is that nothing is ever published to rt/arm_sdk, so a
finger experiment carries no arm risk at all -- and, as a side effect, the
process is not competing with a 50 Hz Python-CRC publish loop and a second
35-motor lowstate deserialisation, which is what starves the socket loop.
"""
import os
import sys
import threading
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "onboard"))
# bridge/ too, for conftest's autouse fixture, which imports retarget_g1. This
# module only ever passed because some alphabetically earlier test happened to
# put bridge/ on the path first -- run it alone and it errored. Adding a test
# file whose name sorts before this one was enough to break it.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))
import g1_onboard as ob  # noqa: E402


class FakeHand:
    # **kw because Dex3Sender takes kp/kd now: the finger stiffness became a
    # deploy-time variable when the fingers started reporting back and the
    # knuckle joints turned out to move 5-50 % of what they are told.
    def __init__(self, **kw):
        self.gains = kw
        self.grips = []
        self.relaxed = 0

    def ctrl_dual_hand(self, l, r):
        self.grips.append((list(l), list(r)))

    def relax(self):
        self.relaxed += 1


@pytest.fixture
def cmd(monkeypatch):
    stub = types.ModuleType("g1_dex3")
    stub.Dex3Sender = FakeHand
    monkeypatch.setitem(sys.modules, "g1_dex3", stub)
    # an arm module that EXPLODES if anything tries to construct it
    boom = types.ModuleType("g1_arm")
    def _never(*a, **k):
        raise AssertionError("hands-only mode constructed the arm controller")
    boom.G1_29_ArmController = _never
    monkeypatch.setitem(sys.modules, "g1_arm", boom)

    c = ob.Commander.__new__(ob.Commander)
    c.lock = threading.RLock()
    c.state = "STANDBY"
    c.arm = c.hand = None
    c.hands_only = False
    c.discover = False
    c._busy = False
    c.homing = False
    c.lowstate_t = 0.0
    c.measured_arm_q = [0.0] * 14
    c.last_rx_t = 0.0
    c._last_cmd = None
    c._last_cmd_t = 0.0
    c.last_apply_t = 0.0
    c.last_frame_t = 0.0
    c.deadman_bit = None
    c.remote_t = 0.0
    c._release_seen = True
    c.deadman_held = False
    c.n_applied = c.n_rejected = c.n_watchdog = c.n_armed = c.n_leashed = 0
    c._leash_log_t = 0.0
    c._track_err = []
    c.arm_cmd_ms = c.hand_cmd_ms = None
    c.last_freeze_why = ""
    c.n_lowstate = 0
    c.n_rx = 0
    c._rx_age = []
    c._rx_min = None
    c._rx_min_proc = None
    c._rx_baseline_state = None
    c.cal = None
    c.home_q = None
    c.on_lowstate(types.SimpleNamespace(wireless_remote=None, motor_state=None))
    return c


def test_take_hands_never_builds_the_arm_controller(cmd):
    """The fake g1_arm raises if touched; reaching HOLDING proves it wasn't."""
    reply = cmd.take_hands()
    assert "HANDS ONLY" in reply
    assert cmd.state == "HOLDING"
    assert cmd.arm is None, "an arm controller was created"
    assert cmd.hands_only is True


def test_armed_hands_only_commands_fingers_and_never_the_arms(cmd):
    cmd.take_hands()
    cmd.last_rx_t = ob.time.time()
    assert cmd.arm_on().startswith("ARMED")
    grip = [0.3] * 7
    cmd.apply([0.9] * 14, grip, grip)
    assert cmd.hand.grips, "no finger command was sent"
    assert cmd.arm is None, "the arms were touched"


def test_finger_targets_are_still_clamped_per_hand(cmd):
    """The URDF limits are mirrored; hands-only must not bypass them.

    Clamping lives in sanitize_hand, which the WebSocket handler calls before
    apply() -- so this walks the real path rather than calling apply() with raw
    values, which would test nothing.
    """
    cmd.take_hands()
    cmd.last_rx_t = ob.time.time()
    cmd.arm_on()
    left_in = ob.sanitize_hand([99.0] * 7, "left")
    right_in = ob.sanitize_hand([-99.0] * 7, "right")
    cmd.apply([0.0] * 14, left_in, right_in)
    left, right = cmd.hand.grips[-1]
    for i, v in enumerate(left):
        lo, hi = ob.DEX3_LIMITS["left"][i]
        assert lo - 1e-9 <= v <= hi + 1e-9, f"left[{i}]={v} outside {lo}..{hi}"
    for i, v in enumerate(right):
        lo, hi = ob.DEX3_LIMITS["right"][i]
        assert lo - 1e-9 <= v <= hi + 1e-9, f"right[{i}]={v} outside {lo}..{hi}"
    # and the two hands must not have been given the same numbers
    assert left != right, "mirrored limits collapsed to one table"


def test_hold_and_relax_work_with_no_arm(cmd):
    cmd.take_hands()
    assert "HOLDING" in cmd.hold()
    assert cmd.hand.relaxed == 1, "hands were not relaxed on hold"
    assert "STANDBY" in cmd.relax(go_home=False)
    assert cmd.hands_only is False, "the mode leaked past release"


def test_arming_still_refuses_without_hand_data(cmd):
    cmd.take_hands()
    cmd.last_rx_t = 0.0
    assert cmd.arm_on().startswith("refused")


def test_take_hands_refuses_while_the_arms_are_held(cmd):
    cmd.arm = object()
    assert cmd.take_hands().startswith("refused")


def test_a_frame_with_no_arm_key_commands_only_the_fingers(cmd):
    """Fingers-only puts NO arm joints on the wire. The onboard must accept
    such a frame and command only the hands -- that absence is the wire-level
    guarantee that nothing can move an arm in this mode."""
    cmd.take_hands()
    cmd.last_rx_t = ob.time.time()
    cmd.arm_on()
    cmd.apply(None, [0.2] * 7, [-0.2] * 7)      # arm14 is None: no arm data
    assert cmd.hand.grips, "fingers were not commanded"
    assert cmd.arm is None


def test_sanitize_is_skipped_cleanly_when_there_is_no_arm_data():
    """sanitize_arm must not be called on a missing key."""
    import inspect
    # Pose application now runs in the newest-wins worker, not inline in the
    # socket reader. Inspect the actual sanitization seam.
    src = inspect.getsource(ob._prepare_target_frame)
    assert 'if "arm" in frame:' in src and '"arm_partial" in frame' in src, \
        "a fingers-only frame would raise on the missing arm key"
    assert "arm = None" in src, "no path leaves the arm uncommanded"
