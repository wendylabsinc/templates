"""Safety tests for the onboard commander.

Every test here corresponds to a finding in REVIEW-g1_onboard.md. They run with
no robot and no DDS stack, which is the point: safety code that can only be
exercised on hardware does not get exercised.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "onboard"))

import g1_onboard as ob  # noqa: E402


class FakeArm:
    """Records what it was told to do; never touches anything."""

    def __init__(self):
        self.commands = []
        self.homed = 0
        self.q = [0.0] * ob.ARM_DIM

    def ctrl_dual_arm(self, q, dq):
        self.commands.append(list(q))

    def get_current_dual_arm_q(self):
        return list(self.q)

    def ctrl_dual_arm_go_home(self):
        self.homed += 1


    def set_arm_compliance(self, kp=None, kd=None):
        self.compliance = getattr(self, "compliance", [])
        self.compliance.append((kp, kd))


class FakeHand:
    # **kw because Dex3Sender takes kp/kd now: the finger stiffness became a
    # deploy-time variable when the fingers started reporting back and the
    # knuckle joints turned out to move 5-50 % of what they are told.
    def __init__(self, **kw):
        self.gains = kw
        self.grips = []
        self.relaxed = 0

    def ctrl_dual_hand(self, left, right):
        self.grips.append((list(left), list(right)))

    def relax(self):
        self.relaxed += 1


def armed_commander():
    c = ob.Commander()
    c.arm, c.hand = FakeArm(), FakeHand()
    c.state = "HOLDING"
    # Arming now requires live hand data: arming with none set ARMED and let the
    # watchdog freeze it 0.3 s later, which on hardware read as "armed but not
    # following". Tests that want an armed commander must say data is arriving.
    c.last_rx_t = time.time()
    c.arm_on()
    return c


class InputValidationTests(unittest.TestCase):
    """BLOCKER 1 — any value reached the arm."""

    def test_nan_is_refused_not_clamped(self):
        with self.assertRaises(ValueError):
            ob.sanitize_arm([float("nan")] + [0.0] * 13)

    def test_infinity_is_refused(self):
        with self.assertRaises(ValueError):
            ob.sanitize_arm([float("inf")] + [0.0] * 13)

    def test_bare_nan_in_json_is_refused(self):
        """json.loads accepts bare NaN by default; that must not reach a motor."""
        with self.assertRaises(ValueError):
            json.loads('{"arm": [NaN]}', parse_constant=ob._reject_nonfinite)

    def test_out_of_range_targets_are_clamped_to_joint_limits(self):
        out = ob.sanitize_arm([100.0] * ob.ARM_DIM)
        for value, (lo, hi) in zip(out, ob.ARM_LIMITS):
            self.assertLessEqual(value, hi)
            self.assertGreaterEqual(value, lo)

    def test_shoulder_roll_limits_are_mirrored_between_arms(self):
        """The G1's roll limits are not symmetric; a shared table would drive
        one arm into its own torso."""
        self.assertEqual(ob.ARM_LIMITS[1], (-1.5882, 2.2515))   # left
        self.assertEqual(ob.ARM_LIMITS[8], (-2.2515, 1.5882))   # right

    def test_wrong_length_is_refused(self):
        for bad in ([], [0.0] * 13, [0.0] * 15, "fourteen", None):
            with self.assertRaises((ValueError, TypeError)):
                ob.sanitize_arm(bad)


class SingleOperatorTests(unittest.TestCase):
    """BLOCKER 2 — a second client's disconnect released the arms."""

    def test_only_one_operator_can_claim(self):
        c = ob.Commander()
        self.assertIsNotNone(c.claim())
        self.assertIsNone(c.claim())

    def test_a_non_owner_cannot_release(self):
        c = ob.Commander()
        owner = c.claim()
        self.assertFalse(c.release("someone-else"))
        self.assertTrue(c.release(owner))

    def test_control_is_reclaimable_after_release(self):
        c = ob.Commander()
        t = c.claim()
        c.release(t)
        self.assertIsNotNone(c.claim())


class NoSpeedRampTests(unittest.TestCase):
    """The controller's 20->30 rad/s ramp is gone; arming must not depend on it.

    It used to be started here rather than at take_control, after it kept
    expiring while the operator put the headset on. What bounds the arming
    snap is ARM_SLEW_RAD_S on the commanded targets, ten times tighter, and
    that is tested in test_slew_and_control.
    """

    def test_arming_does_not_call_a_speed_ramp(self):
        c = ob.Commander()
        c.arm, c.hand = FakeArm(), FakeHand()   # FakeArm has no speed_gradual_max
        c.state = "HOLDING"
        c.last_rx_t = time.time()
        self.assertTrue(c.arm_on().startswith("ARMED"))
        self.assertEqual(c.state, "ARMED")


class HoldTests(unittest.TestCase):
    """HIGH — hold() left the hands gripping."""

    def test_hold_relaxes_the_hands(self):
        c = armed_commander()
        c.hold()
        self.assertEqual(c.hand.relaxed, 1)
        self.assertEqual(c.state, "HOLDING")

    def test_hold_freezes_the_arms(self):
        c = armed_commander()
        before = len(c.arm.commands)
        c.hold()
        self.assertGreater(len(c.arm.commands), before)

    def test_hold_serializes_with_an_inflight_independent_hand_write(self):
        """A decoupled Dex3 worker must not write after HOLD relaxes it."""
        class SlowHand(FakeHand):
            def __init__(self):
                super().__init__()
                self.started = threading.Event()
                self.release_write = threading.Event()

            def ctrl_dual_hand(self, left, right):
                self.started.set()
                self.release_write.wait(1.0)
                super().ctrl_dual_hand(left, right)

        c = armed_commander()
        c.hand = SlowHand()
        write = threading.Thread(
            target=c.apply_hands, args=([0.1] * 7, [0.1] * 7),
            kwargs={"seq": 1})
        write.start()
        self.assertTrue(c.hand.started.wait(0.2))

        held = []
        stop = threading.Thread(target=lambda: held.append(c.hold()))
        stop.start()
        time.sleep(0.02)
        self.assertTrue(stop.is_alive(), "HOLD raced the active Dex3 write")
        self.assertEqual(c.hand.relaxed, 0)

        c.hand.release_write.set()
        write.join(0.5)
        stop.join(0.5)
        self.assertFalse(write.is_alive())
        self.assertFalse(stop.is_alive())
        self.assertEqual(c.state, "HOLDING")
        self.assertEqual(c.hand.relaxed, 1)
        self.assertTrue(held[0].startswith("HOLDING"))

        # A queued worker that wakes after the transition sees non-ARMED and
        # refuses before touching the hand again.
        self.assertFalse(c.apply_hands([0.2] * 7, [0.2] * 7, seq=2))
        self.assertEqual(len(c.hand.grips), 1)


class DisconnectTests(unittest.TestCase):
    """MEDIUM — go_home ran on an unexplained disconnect."""

    def test_relax_does_not_home_by_default(self):
        c = armed_commander()
        c.relax()
        self.assertEqual(c.arm, None)
        self.assertEqual(c.state, "STANDBY")

    def test_relax_homes_only_when_explicitly_asked(self):
        c = armed_commander()
        arm = c.arm
        c.relax(go_home=True)
        self.assertEqual(arm.homed, 1)

    def test_default_relax_leaves_the_arm_where_it_was(self):
        c = armed_commander()
        arm = c.arm
        c.relax()
        self.assertEqual(arm.homed, 0)


class ArmedGateTests(unittest.TestCase):
    def test_targets_are_ignored_unless_armed(self):
        c = ob.Commander()
        c.arm, c.hand = FakeArm(), FakeHand()
        c.state = "HOLDING"
        c.apply([0.1] * ob.ARM_DIM, [0.0] * 7, [0.0] * 7)
        self.assertEqual(c.arm.commands, [])

    def test_targets_apply_once_armed(self):
        c = armed_commander()
        c.apply([0.1] * ob.ARM_DIM, [0.0] * 7, [0.0] * 7)
        self.assertEqual(len(c.arm.commands), 1)

    def test_a_flooding_sender_is_rate_limited(self):
        c = armed_commander()
        for _ in range(50):
            c.apply([0.1] * ob.ARM_DIM, [0.0] * 7, [0.0] * 7)
        self.assertLess(len(c.arm.commands), 50)


class DefaultsTests(unittest.TestCase):
    def test_a_fresh_commander_touches_nothing(self):
        c = ob.Commander()
        self.assertEqual(c.state, "STANDBY")
        self.assertIsNone(c.arm)
        self.assertIsNone(c.hand)

    def test_arm_on_refuses_without_take_control(self):
        c = ob.Commander()
        self.assertIn("refused", c.arm_on())
        self.assertEqual(c.state, "STANDBY")

    def test_arm_on_refuses_without_hand_data(self):
        """Arming into an instant freeze is worse than refusing to arm."""
        c = ob.Commander()
        c.arm, c.hand = FakeArm(), FakeHand()
        c.state = "HOLDING"
        self.assertIn("refused", c.arm_on())
        self.assertEqual(c.state, "HOLDING")


if __name__ == "__main__":
    unittest.main()


class StateHonestyTests(unittest.TestCase):
    """A safety state machine must not report a state it is not in."""

    def test_hold_from_standby_is_refused(self):
        c = ob.Commander()
        msg = c.hold()
        self.assertIn("refused", msg)
        self.assertEqual(c.state, "STANDBY",
                         "reporting HOLDING with no arms held tells the operator "
                         "the arms are grabbed when they are not")

    def test_hold_works_once_control_is_taken(self):
        c = armed_commander()
        self.assertIn("HOLDING", c.hold())
        self.assertEqual(c.state, "HOLDING")


class DeadmanTests(unittest.TestCase):
    """The dead-man reads the ROBOT'S OWN remote from rt/lowstate, so nothing
    in the headset, browser, laptop or network path can hold it down."""

    def test_unconfigured_deadman_does_not_block(self):
        """Simulator use; the startup banner warns loudly instead."""
        c = ob.Commander()
        self.assertTrue(c.deadman_ok())

    def test_configured_deadman_blocks_until_held(self):
        c = ob.Commander()
        c.deadman_bit = ob.REMOTE_BITS["R2"]
        c.remote_t = 0.0
        self.assertFalse(c.deadman_ok(), "never seen the remote => must not enable motion")

    def test_stale_lowstate_counts_as_released(self):
        import time as _t
        c = ob.Commander()
        c.deadman_bit = ob.REMOTE_BITS["R2"]
        c.deadman_held = True
        c.remote_t = _t.time() - (ob.REMOTE_STALE_S * 2)
        self.assertFalse(c.deadman_ok(),
                         "if we cannot see the remote we do not get to assume it is held")

    def test_targets_are_dropped_while_the_deadman_is_released(self):
        c = armed_commander()
        c.deadman_bit = ob.REMOTE_BITS["R2"]
        c.remote_t = 0.0                       # never seen => released
        c.apply([0.1] * ob.ARM_DIM, [0.0] * 7, [0.0] * 7)
        self.assertEqual(c.arm.commands, [])

    def test_discovery_mode_cannot_take_control(self):
        """Discovery runs on the REAL robot before a dead-man exists, so it must
        not be able to grab the arms."""
        c = ob.Commander()
        c.discover = True
        self.assertIn("refused", c.take_control())
        self.assertIsNone(c.arm)
