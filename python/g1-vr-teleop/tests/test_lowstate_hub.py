"""One rt/lowstate reader for the onboard process (real/lowstate_hub.py).

2026-09-19: the operator heard the motors "fighting". Two Python readers of the
same ~1 kHz topic had starved the 50 Hz arm publisher of the GIL (loop 43.5 Hz,
CRC 21.7 ms against a normal 0.7 ms), the arm's reader thread outlived every
release, and the leash clamped against a measurement 110-145 ms stale.
"""
import importlib
import math
import os
import pathlib
import sys
import threading
import time
import types

import numpy as np
import pytest

REAL = pathlib.Path(__file__).parent.parent / "onboard"
sys.path.insert(0, str(REAL))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "teleop" / "bridge"))
import lowstate_hub as lh                                        # noqa: E402
import g1_onboard as ob                                          # noqa: E402
import dex3_stall as ds                                          # noqa: E402


def _lowstate(mode_machine=5, q=0.0):
    motors = [types.SimpleNamespace(q=q, dq=0.0, tau_est=0.0) for _ in range(35)]
    return types.SimpleNamespace(motor_state=motors, mode_machine=mode_machine,
                                 wireless_remote=None, tick=0)


# ---- the hub ---------------------------------------------------------------------

def test_it_keeps_the_newest_sample_and_hands_it_to_every_listener_in_order():
    samples = [_lowstate(q=0.1), _lowstate(q=0.2)]
    hub = lh.LowstateHub(lambda: samples.pop(0) if samples else None, hz=200)
    seen = []
    hub.add_listener(lambda m: seen.append(("a", m.motor_state[15].q)))
    hub.add_listener(lambda m: seen.append(("b", m.motor_state[15].q)))
    assert hub.latest() == (0, None, None)
    assert hub.poll_once() and hub.poll_once()
    seq, msg, t = hub.latest()
    assert seq == 2 and msg.motor_state[15].q == 0.2 and t is not None
    assert seen == [("a", 0.1), ("b", 0.1), ("a", 0.2), ("b", 0.2)]
    assert not hub.poll_once(), "no sample must not count as one"
    assert hub.latest()[0] == 2


def test_a_listener_that_raises_does_not_stop_the_others():
    hub = lh.LowstateHub(lambda: _lowstate(), hz=200)
    got = []
    hub.add_listener(lambda m: 1 / 0)
    hub.add_listener(lambda m: got.append(m))
    hub.poll_once()
    assert got and hub.n_listener_errors == 1


def test_the_thread_polls_at_its_rate_and_stops():
    hub = lh.LowstateHub(lambda: _lowstate(), hz=200).start()
    time.sleep(0.2)
    hub.stop()
    time.sleep(0.02)
    n = hub.n_samples
    assert 10 <= n <= 60, f"{n} samples in 0.2 s at 200 Hz"


def test_poll_rate_from_env():
    assert lh.poll_hz_from_env({}) == 200.0
    assert lh.poll_hz_from_env({"G1_LOWSTATE_HZ": "100"}) == 100.0
    for bad in ("5", "5000", "fast"):
        with pytest.raises(ValueError):
            lh.poll_hz_from_env({"G1_LOWSTATE_HZ": bad})


def test_main_uses_the_hub_and_no_per_sample_listener():
    """The listener decoded every published sample; the hub decodes one per poll."""
    src = (REAL / "g1_onboard.py").read_text()
    main = src[src.index("def main("):]
    assert "Init(cmd.on_lowstate" not in main, "the per-sample lowstate listener is back"
    assert "LowstateHub(" in main and "hub.add_listener(cmd.on_lowstate)" in main
    assert "lowstate_hub=getattr(self, \"lowstate_hub\", None)" in src, \
        "take_control no longer hands the hub to the arm controller"
    assert ob.LOWSTATE_DECIMATE == 1, "the hub already rate-limits; decimating again starves the leash"


def test_on_lowstate_timestamps_the_measurement():
    c = ob.Commander.__new__(ob.Commander)
    c.lock = threading.RLock()
    c.n_lowstate = 0
    c.cal = None
    c.measured_arm_q = None
    before = time.monotonic()
    c.on_lowstate(_lowstate(q=0.3))
    assert c.measured_arm_q == [0.3] * 14
    assert before <= c.measured_arm_t <= time.monotonic()


# ---- the arm controller reads from it, and its reader stops ----------------------

class _Subscriber:
    made = []

    def __init__(self, topic, typ):
        _Subscriber.made.append(topic)
        self._queue = [_lowstate()]

    def Init(self):
        pass

    def Read(self):
        return self._queue.pop(0) if self._queue else None


class _Publisher:
    def __init__(self, topic, typ):
        pass

    def Init(self):
        pass

    def Write(self, msg):
        pass


class _CRC:
    def Crc(self, msg):
        return 0


def _lowcmd():
    motors = [types.SimpleNamespace(mode=0, q=0.0, dq=0.0, tau=0.0, kp=0.0, kd=0.0)
              for _ in range(35)]
    return types.SimpleNamespace(motor_cmd=motors, mode_pr=0, mode_machine=0, crc=0)


@pytest.fixture
def g1_arm(monkeypatch):
    """real/g1_arm.py against a fake unitree_sdk2py (as test_mode_machine_race)."""
    def mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, name, m)

    mod("unitree_sdk2py")
    mod("unitree_sdk2py.core")
    mod("unitree_sdk2py.core.channel", ChannelPublisher=_Publisher,
        ChannelSubscriber=_Subscriber, ChannelFactoryInitialize=lambda *a, **k: None)
    mod("unitree_sdk2py.idl")
    mod("unitree_sdk2py.idl.unitree_hg")
    mod("unitree_sdk2py.idl.unitree_hg.msg")
    mod("unitree_sdk2py.idl.unitree_hg.msg.dds_", LowCmd_=object, LowState_=object)
    mod("unitree_sdk2py.idl.unitree_go")
    mod("unitree_sdk2py.idl.unitree_go.msg")
    mod("unitree_sdk2py.idl.unitree_go.msg.dds_", LowCmd_=object, LowState_=object)
    mod("unitree_sdk2py.idl.default", unitree_hg_msg_dds__LowCmd_=_lowcmd,
        unitree_go_msg_dds__LowCmd_=_lowcmd)
    mod("unitree_sdk2py.utils")
    mod("unitree_sdk2py.utils.crc", CRC=_CRC)
    monkeypatch.syspath_prepend(str(REAL))
    monkeypatch.delitem(sys.modules, "g1_arm", raising=False)
    monkeypatch.setenv("G1_CONTROL_HZ", "50")
    _Subscriber.made = []
    return importlib.import_module("g1_arm")


def test_with_a_hub_the_arm_controller_opens_no_reader_of_its_own(g1_arm):
    hub = lh.LowstateHub(lambda: _lowstate(mode_machine=7, q=0.25), hz=200)
    hub.poll_once()
    arm = g1_arm.G1_29_ArmController(motion_mode=True, lowstate_hub=hub)
    try:
        assert "rt/lowstate" not in _Subscriber.made, "a second lowstate reader was opened"
        assert arm.get_mode_machine() == 7
        assert np.allclose(arm.get_current_dual_arm_q(), 0.25)
        # A new sample reaches it through the hub.
        hub._read = lambda: _lowstate(mode_machine=7, q=0.5)
        hub.poll_once()
        deadline = time.time() + 1.0
        while not np.allclose(arm.get_current_dual_arm_q(), 0.5) and time.time() < deadline:
            time.sleep(0.005)
        assert np.allclose(arm.get_current_dual_arm_q(), 0.5)
    finally:
        arm.shutdown(ramp_s=0.0)
    assert not arm.subscribe_thread.is_alive(), "the hub-fed reader outlived shutdown"


def test_without_a_hub_the_vendored_reader_still_stops_on_shutdown(g1_arm):
    """Upstream it was `while True`: one leaked reader per take_control/relax."""
    arm = g1_arm.G1_29_ArmController(motion_mode=True)
    assert "rt/lowstate" in _Subscriber.made
    arm.shutdown(ramp_s=0.0)
    assert not arm.subscribe_thread.is_alive(), "the lowstate reader outlived shutdown"


# ---- the leash widens with the measurement's age -----------------------------------

def test_the_band_is_the_limit_plus_what_the_arm_can_have_moved_since():
    L, s = ob.MAX_TRACKING_ERROR_RAD, ob.LEASH_AGE_RATE_RAD_S
    assert ob.leash_band(None, 10.0) == L, "no timestamp must mean exactly the old limit"
    assert ob.leash_band(10.0, 10.0) == L
    assert ob.leash_band(10.0 - 0.1, 10.0) == pytest.approx(L + s * 0.1)
    assert ob.leash_band(10.0 - 5.0, 10.0) == pytest.approx(L + s * ob.LEASH_AGE_CAP_S)
    assert ob.leash_band(10.0 + 1.0, 10.0) == L, "a future stamp must not narrow it"


class _FakeArm:
    def __init__(self):
        self.sent = []

    def get_current_dual_arm_q(self):
        return np.zeros(14)

    def ctrl_dual_arm(self, q, tau):
        self.sent.append(np.asarray(q, float).copy())


def _leash_cmd(measured_age_s):
    c = ob.Commander.__new__(ob.Commander)
    c.measured_arm_q = [0.0] * 14
    c.measured_arm_t = time.monotonic() - measured_age_s
    c.n_leashed = 0
    c._leash_log_t = 0.0
    c._track_err = []
    c._last_cmd = None
    return c


def test_a_stale_measurement_no_longer_trips_the_leash():
    """12 deg ahead of a reading 120 ms old: the arm may have moved up to
    17 deg since (2.5 rad/s x 0.12 s), so this is lag, not a blocked joint."""
    q = np.full(14, math.radians(12.0))
    stale = _leash_cmd(0.12)
    assert np.allclose(stale._leash(q.copy(), time.time()), q)
    assert stale.n_leashed == 0


def test_a_fresh_measurement_still_clamps_a_blocked_joint():
    q = np.full(14, math.radians(12.0))
    fresh = _leash_cmd(0.0)
    out = fresh._leash(q.copy(), time.time())
    assert fresh.n_leashed == 1
    assert np.all(out <= ob.MAX_TRACKING_ERROR_RAD + ob.LEASH_AGE_RATE_RAD_S * 0.01 + 1e-9)


def test_however_stale_the_band_is_bounded():
    q = np.full(14, math.radians(60.0))
    c = _leash_cmd(10.0)
    out = c._leash(q.copy(), time.time())
    assert c.n_leashed == 1
    assert np.all(out <= ob.MAX_TRACKING_ERROR_RAD + ob.LEASH_AGE_RATE_RAD_S * ob.LEASH_AGE_CAP_S + 1e-9)


def test_faster_smoothing_does_not_widen_the_tracking_leash():
    assert ob.ARM_SLEW_RAD_S == 35.0
    assert ob.LEASH_AGE_RATE_RAD_S == 3.5
    assert ob.leash_band(0.0, 10.0) == pytest.approx(
        ob.MAX_TRACKING_ERROR_RAD + ob.LEASH_AGE_RATE_RAD_S * ob.LEASH_AGE_CAP_S)


# ---- fingers stopped short are backed off like any other stall -------------------

def test_a_thumb_stopped_0p21_short_is_backed_off_to_the_margin():
    """Empty hand at full trigger: thumb_1 stops ~0.21 rad short against the
    closed fingers. At a 0.3 gap threshold it pushed with all 0.21 forever."""
    t = [0.0]
    g = ds.StallBackoff(clock=lambda: t[0])
    assert g.gap_rad == pytest.approx(g.margin_rad), "the stall gap must not exceed the hold margin"
    cmd = [0.0, 0.92, 0.0, 0.0, 0.0, 0.0, 0.0]            # left thumb_1 fully closed
    meas = [0.0, 0.92 - 0.21, 0.0, 0.0, 0.0, 0.0, 0.0]    # stopped 0.21 short
    g.apply("left", cmd, meas)
    t[0] += 1.0
    out, names = g.apply("left", cmd, meas)
    assert names == ["thumb_1"]
    assert out[1] == pytest.approx(0.92 - 0.21 + g.margin_rad)
    assert ds.StallBackoff.from_env({}).gap_rad == pytest.approx(0.15)
