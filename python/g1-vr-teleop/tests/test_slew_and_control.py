"""The two changes that made unattended teleop possible: a slew limit on the
first commanded frame, and a control surface that is not stdin."""
import os
import sys
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "onboard"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))


# --------------------------------------------------------------------------- #
# slew limit
# --------------------------------------------------------------------------- #
class FakeArm:
    """Records what actually reached the controller."""
    def __init__(self, q):
        self.q = np.asarray(q, float)
        self.sent = []

    def get_current_dual_arm_q(self):
        return self.q.copy()

    def ctrl_dual_arm(self, q, tau):
        self.sent.append(np.asarray(q, float).copy())


@pytest.fixture
def cmd():
    import g1_onboard
    c = g1_onboard.Commander.__new__(g1_onboard.Commander)   # no DDS, no threads
    c.motion_limits = {"max_velocity_rad_s": 3.5, "max_acceleration_rad_s2": 16.}
    c.lock = __import__("threading").RLock()
    c.state = "ARMED"
    c.hand = None
    c.deadman_bit = None
    c.remote_t = 0.0
    c.discover = False
    c._release_seen = True
    c.deadman_held = False
    c._last_cmd = None
    c._last_cmd_t = 0.0
    c.lowstate_t = 0.0
    c._busy = False
    c.n_rx = 0
    c._rx_age = []
    c.measured_arm_q = None
    c.n_lowstate = 0
    c.arm_cmd_ms = None
    c.hand_cmd_ms = None
    c.n_applied = 0
    c.n_rejected = 0
    c.n_watchdog = 0
    c.n_armed = 0
    c.last_freeze_why = ""
    c.arm = None
    c.owner = None
    c.last_apply_t = 0.0
    c.last_frame_t = 0.0
    return c


def test_first_frame_after_arming_does_not_snap(cmd):
    """The bug this exists for: arm pointing down, hands up, one frame."""
    import g1_onboard
    cmd.arm = FakeArm(np.zeros(14))
    far = [1.5] * 14                      # ~86 deg away on every joint
    cmd.apply(far, [0.0] * 7, [0.0] * 7)
    sent = cmd.arm.sent[-1]
    # One frame may move at most ARM_SLEW_RAD_S * dt, and dt is clamped to 0.1s.
    assert np.max(np.abs(sent)) <= g1_onboard.ARM_SLEW_RAD_S * 0.1 + 1e-9
    assert np.max(np.abs(sent)) < 0.5, "a 1.5 rad step reached the motors"


def test_slew_converges_to_the_target(cmd):
    cmd.arm = FakeArm(np.zeros(14))
    target = [0.4] * 14
    for _ in range(400):
        cmd.last_apply_t = 0.0             # bypass the rate gate
        cmd.apply(target, [0.0] * 7, [0.0] * 7)
    assert np.allclose(cmd.arm.sent[-1], 0.4, atol=1e-6)


def test_slew_reseeds_from_measurement_after_a_freeze(cmd):
    """A freeze must not leave a stale target that resumes with a jump."""
    cmd.arm = FakeArm(np.zeros(14))
    cmd.apply([1.0] * 14, [0.0] * 7, [0.0] * 7)
    with cmd.lock:
        cmd._freeze()
    assert cmd._last_cmd is None
    cmd.arm.q = np.full(14, -0.8)          # someone moved the arms meanwhile
    cmd.last_apply_t = 0.0
    cmd.apply([1.0] * 14, [0.0] * 7, [0.0] * 7)
    assert np.max(np.abs(cmd.arm.sent[-1] - (-0.8))) < 0.3


def test_joint_limits_still_apply_through_the_slew(cmd):
    import g1_onboard
    # effective, not mechanical: a bundled or captured calibration narrows
    # these, and sanitize_arm must follow whichever is in force.
    lo, hi = g1_onboard.effective_arm_limits()[0]
    assert g1_onboard.sanitize_arm([hi + 5.0] + [0.0] * 13)[0] == pytest.approx(hi)
    assert g1_onboard.sanitize_arm([lo - 5.0] + [0.0] * 13)[0] == pytest.approx(lo)


# --------------------------------------------------------------------------- #
# control surface
# --------------------------------------------------------------------------- #
@pytest.fixture
def sink(monkeypatch):
    import sink_realg1
    monkeypatch.setattr(sink_realg1.threading, "Thread",
                        lambda *a, **k: types.SimpleNamespace(start=lambda: None))
    return sink_realg1.RealG1Sink("ws://robot:8090/ctrl?key=secret")


def test_command_queues_only_known_commands(sink):
    sink.connected = True
    assert sink.command("arm") is True
    assert sink.command("take_control") is True
    assert sink.command("speak", payload={"prompt": "thank_you"}) is True
    assert sink.command("launch_missiles") is False
    assert sink._cmd_q == ["arm", "take_control",
                           {"cmd": "speak", "prompt": "thank_you"}]


def test_arm_is_not_queued_while_disconnected(sink):
    assert sink.command("arm") is False
    assert sink._cmd_q == []


def test_disconnect_discards_pending_arm_but_preserves_stop(sink):
    sink.connected = True
    sink._cmd_q = ["arm", {"cmd": "arm"}, "hold", "relax"]
    sink._connection_lost()
    assert sink.connected is False
    assert sink._cmd_q == ["hold", "relax"]
    assert sink.command("arm") is False


def test_status_never_echoes_the_shared_secret(sink):
    """The control page is open to anyone who can reach the bridge."""
    assert "secret" not in repr(sink.status())


def test_status_reports_disconnected_before_anything_connects(sink):
    s = sink.status()
    assert s["connected"] is False
    assert s["hands_fresh"] is False


def test_realg1_sink_without_a_url_is_refused():
    """--sink realg1 with no URL discarded every frame in silence."""
    import server
    with pytest.raises(SystemExit):
        sys.argv = ["server.py", "--sink", "realg1"]
        server.main()


# --------------------------------------------------------------------------- #
# take_control must answer, never hang
#
# The vendored arm controller's constructor spins on an unbounded
# `while not lowstate: sleep(0.1)` in the caller's thread. take_control called
# it straight from the async WebSocket handler, so a robot that was not
# publishing lowstate -- exactly what "not in Main Operation Control" looks
# like -- froze the whole server: no /status, no hold, no relax.
# --------------------------------------------------------------------------- #
def test_lowstate_liveness_survives_a_missing_remote_payload(cmd):
    """A lowstate with no wireless_remote still proves the robot is alive."""
    cmd.deadman_bit = None
    cmd.on_lowstate(types.SimpleNamespace(wireless_remote=None))
    assert cmd.lowstate_ok() is True


def test_lowstate_ok_is_false_before_anything_arrives(cmd):
    assert cmd.lowstate_ok() is False


def test_lowstate_ok_goes_false_when_the_robot_stops_talking(cmd):
    import g1_onboard
    cmd.on_lowstate(types.SimpleNamespace(wireless_remote=None))
    cmd.lowstate_t = time.time() - (g1_onboard.LOWSTATE_STALE_S + 0.5)
    assert cmd.lowstate_ok() is False


def test_take_control_refuses_instead_of_hanging_with_no_lowstate(cmd):
    cmd.state = "STANDBY"
    cmd.discover = False
    reply = cmd.take_control()             # must return, and fast
    assert reply.startswith("refused")
    assert "Main Operation Control" in reply
    assert cmd.arm is None and cmd.state == "STANDBY"


def test_take_control_refuses_in_discovery_mode(cmd):
    cmd.discover = True
    cmd.on_lowstate(types.SimpleNamespace(wireless_remote=None))
    assert cmd.take_control().startswith("refused")


def test_a_failed_grab_is_reported_not_raised(cmd, monkeypatch):
    """A controller that throws must not take the server down with it."""
    boom = types.ModuleType("g1_arm")
    def _raise(*a, **k):
        raise TimeoutError("no lowstate after 5 s")
    boom.G1_29_ArmController = _raise
    monkeypatch.setitem(sys.modules, "g1_arm", boom)
    stub = types.ModuleType("g1_dex3")
    stub.Dex3Sender = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "g1_dex3", stub)

    cmd.state = "STANDBY"
    cmd.discover = False
    cmd.on_lowstate(types.SimpleNamespace(wireless_remote=None))
    reply = cmd.take_control()
    assert reply.startswith("refused: could not grab the arms")
    assert cmd.arm is None and cmd.state == "STANDBY"
    assert cmd._busy is False, "a failed grab left take_control permanently blocked"


def test_the_vendored_wait_is_bounded():
    """Guard the patch itself: upstream ships this loop unbounded."""
    src = pathlib.Path(__file__).parent.parent / "onboard" / "g1_arm.py"
    body = src.read_text()
    i = body.index("class G1_29_ArmController")
    j = body.index("def _ctrl_motor_state", i)
    ctor = body[i:j]
    assert "while not self.lowstate_buffer.GetData():" in ctor
    assert "raise TimeoutError" in ctor, "the lowstate wait can hang the server again"


import pathlib   # noqa: E402  (used by the test above)
import time      # noqa: E402


def test_arming_with_no_hand_data_is_refused(cmd):
    """Arming into an instant watchdog freeze is worse than not arming."""
    cmd.arm = FakeArm(np.zeros(14))
    cmd.state = "HOLDING"
    cmd.last_rx_t = 0.0
    reply = cmd.arm_on()
    assert reply.startswith("refused")
    assert "2 hands" in reply
    assert cmd.state == "HOLDING", "armed despite having nothing to follow"


def test_arming_is_allowed_once_frames_are_arriving(cmd):
    cmd.arm = FakeArm(np.zeros(14))
    cmd.state = "HOLDING"
    cmd.last_rx_t = time.time()
    assert cmd.arm_on().startswith("ARMED")
    assert cmd.state == "ARMED"


def test_stale_hand_data_does_not_count_as_arriving(cmd):
    import g1_onboard
    cmd.arm = FakeArm(np.zeros(14))
    cmd.state = "HOLDING"
    cmd.last_rx_t = time.time() - (g1_onboard.RX_REQUIRED_S + 0.4)
    assert cmd.arm_on().startswith("refused")


# --------------------------------------------------------------------------- #
# home
# --------------------------------------------------------------------------- #
def test_take_control_holds_the_measured_pose_not_zero(cmd, monkeypatch):
    """The vendored controller seeds q_target=zeros and starts publishing.

    Without an immediate command at the measured pose, taking control drives
    both arms to the zero pose while reporting 'holding current pose'.
    """
    import g1_onboard
    measured = np.full(14, 0.7)
    arm = FakeArm(measured)
    fake_arm_mod = types.ModuleType("g1_arm")
    fake_arm_mod.G1_29_ArmController = lambda **k: arm
    fake_dex = types.ModuleType("g1_dex3")
    fake_dex.Dex3Sender = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "g1_arm", fake_arm_mod)
    monkeypatch.setitem(sys.modules, "g1_dex3", fake_dex)

    cmd.arm = None
    cmd.state = "STANDBY"
    cmd.discover = False
    cmd.homing = False
    cmd.on_lowstate(types.SimpleNamespace(wireless_remote=None))
    cmd.take_control()

    assert arm.sent, "take_control issued no command; q_target stays at zeros"
    assert np.allclose(arm.sent[0], measured), "commanded something other than where the arms are"


def test_go_home_is_refused_while_armed(cmd):
    cmd.arm = FakeArm(np.zeros(14))
    cmd.state = "ARMED"
    cmd.home_q = [0.3] * 14
    cmd.homing = False
    assert cmd.go_home().startswith("refused")


def test_go_home_is_refused_with_no_home_captured(cmd):
    cmd.arm = FakeArm(np.zeros(14))
    cmd.state = "HOLDING"
    cmd.home_q = None
    cmd.homing = False
    assert "no home captured" in cmd.go_home()


def test_go_home_slews_there_and_stops(cmd):
    cmd.arm = FakeArm(np.zeros(14))
    cmd.state = "HOLDING"
    cmd.home_q = [0.25] * 14
    cmd.homing = False
    assert cmd.go_home().startswith("at home")   # the reply now carries how far off it was
    assert np.allclose(cmd.arm.sent[-1], 0.25, atol=1e-3)
    assert cmd.homing is False
    # and it must arrive gradually, not in one step
    assert len(cmd.arm.sent) > 5, "homed in a single jump"
    assert cmd._last_cmd is None, "teleop slew must re-seed after homing"


def test_set_home_does_not_require_taking_control(cmd, tmp_path, monkeypatch):
    """Home is posed by hand, which is impossible while we hold the arms."""
    import g1_onboard
    monkeypatch.setattr(g1_onboard, "HOME_PATH", str(tmp_path / "home.json"))
    cmd.arm = None                       # nothing grabbed
    cmd.measured_arm_q = [0.4] * 14
    reply = cmd.set_home()
    assert "home captured" in reply
    assert cmd.home_q == [0.4] * 14


def test_set_home_refuses_without_lowstate(cmd):
    cmd.arm = None
    cmd.measured_arm_q = None
    assert cmd.set_home().startswith("refused")


def test_home_survives_a_restart(cmd, tmp_path, monkeypatch):
    import g1_onboard
    path = str(tmp_path / "home.json")
    monkeypatch.setattr(g1_onboard, "HOME_PATH", path)
    cmd.arm = None
    cmd.measured_arm_q = [-0.2] * 14
    cmd.set_home()
    assert g1_onboard._load_home() == [-0.2] * 14
