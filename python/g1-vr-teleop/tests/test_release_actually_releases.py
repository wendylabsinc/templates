"""Release must stop publishing to rt/arm_sdk.

Measured on unitree-g1-nx-2: the onboard reported STANDBY while rt/arm_sdk was
still being published at 28 Hz with weight 1.0 on all 29 joints. The vendored
controller's publish loop is a bare `while True` in a daemon thread, so dropping
the Python reference stopped nothing. The robot's own controller then fought
that stale pose on every mode change -- audible as grinding, and reported by the
operator on 'locked standing'.
"""
import os
import pathlib
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "onboard"))
ARM_SRC = pathlib.Path(__file__).parent.parent / "onboard" / "g1_arm.py"


def _g1_29_source():
    body = ARM_SRC.read_text()
    i = body.index("class G1_29_ArmController")
    j = body.index("class G1_23_ArmController")
    return body[i:j]


def test_the_publish_loop_can_be_stopped():
    src = _g1_29_source()
    assert "while self._running:" in src, "the publish loop is unbounded again"
    assert "while True:" not in src.split("def _ctrl_motor_state")[1].split("def ")[0]


def test_shutdown_exists_and_clears_the_flag_even_if_the_ramp_raises():
    src = _g1_29_source()
    assert "def shutdown" in src
    # The flag must be cleared in a finally: a throwing ramp that skipped it
    # would leave exactly the thread we are trying to kill.
    sd = src.split("def shutdown")[1].split("\n    def ")[0]
    assert "finally:" in sd
    assert "self._running = False" in sd
    assert sd.index("finally:") < sd.index("self._running = False")


def test_the_weight_is_ramped_not_dropped():
    sd = _g1_29_source().split("def shutdown")[1].split("\n    def ")[0]
    assert "linspace(1.0, 0.0" in sd, "handing authority back must be gradual"


def test_relax_calls_shutdown():
    body = (pathlib.Path(__file__).parent.parent / "onboard" / "g1_onboard.py").read_text()
    rel = body.split("def relax")[1].split("\n    def ")[0]
    assert "self.arm.shutdown()" in rel, "release drops the reference but never stops the thread"


class FakeController:
    """A publish thread that only stops when told, like the real one."""
    def __init__(self):
        self._running = True
        self.published = 0
        self.weights = []
        self.control_dt = 1.0 / 250.0
        self.motion_mode = True
        self.publish_thread = threading.Thread(target=self._loop, daemon=True)
        self.publish_thread.start()

    def _loop(self):
        while self._running:
            self.published += 1
            time.sleep(0.001)

    def get_current_dual_arm_q(self):
        return [0.0] * 14

    def ctrl_dual_arm(self, q, tau):
        pass

    def shutdown(self, ramp_s=0.02):
        try:
            for w in (1.0, 0.5, 0.0):
                self.weights.append(w)
        finally:
            self._running = False
            self.publish_thread.join(timeout=2.0)


def test_dropping_the_reference_does_not_stop_the_thread():
    """Characterise the original bug."""
    c = FakeController()
    time.sleep(0.02)
    n = c.published
    del c                      # what relax() used to do: self.arm = None
    import gc
    gc.collect()
    time.sleep(0.02)
    # The thread is still alive; nothing observable stopped it.
    live = [t for t in threading.enumerate() if t.is_alive()]
    assert len(live) >= 1 and n > 0


def test_relax_stops_the_publisher_end_to_end():
    import g1_onboard
    cmd = g1_onboard.Commander.__new__(g1_onboard.Commander)
    cmd.lock = threading.RLock()
    cmd.state = "HOLDING"
    cmd.hand = None
    cmd._last_cmd = None
    cmd._last_cmd_t = 0.0
    cmd.owner = None
    cmd.n_rx = cmd.n_lowstate = 0
    cmd._rx_age = []
    cmd.arm_cmd_ms = cmd.hand_cmd_ms = None
    cmd.n_applied = cmd.n_rejected = cmd.n_watchdog = cmd.n_armed = 0
    cmd.last_freeze_why = ""
    ctrl = FakeController()
    cmd.arm = ctrl
    time.sleep(0.02)

    cmd.relax(go_home=False)

    assert cmd.state == "STANDBY"
    assert ctrl._running is False, "STANDBY was reported while still publishing"
    assert ctrl.publish_thread.is_alive() is False
    assert ctrl.weights and ctrl.weights[-1] == 0.0, "weight was not handed back"
