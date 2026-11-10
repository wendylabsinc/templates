"""Waist yaw on the RIGHT thumbstick: the reach the arm cannot give.

Every reach problem so far was fought inside the arm -- the shoulder-yaw
search exists because the hand could not get across the body, and the
receiver in the coke task sits 42 cm to the side. A waist turn moves the whole
workspace instead of contorting the arm. This robot's waist roll and pitch
report dead (27 of 29 motors live), so it is yaw only: Unitree motor 12.

Driven explicitly, by the stick, not derived from the operator's body: the
"goes somewhere I didn't point" complaint was about motion the operator did
not ask for, and a torso derived from the hands would be exactly that. The
stick value is already on the wire and in every row, so the choice is
recorded, and an offline replay can say later whether a follow mode is worth
it.

Bridge: dead zone, rate integration, cap, reset when the robot leaves ARMED,
never from a tracked hand. Wire: `waist_yaw_joint` in the targets (so the row
records it by name), a top-level `waist` on the sink message (the 14-vector
is untouched). Onboard: its own slew, calibrated clamp, the arm's leash,
ARMED only, motor 12 written only when a waist was commanded.
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))
sys.path.insert(0, str(ROOT / "onboard"))

import g1_onboard as ob  # noqa: E402
import server  # noqa: E402
import waist  # noqa: E402
from sink_realg1 import RealG1Sink  # noqa: E402
from tests.test_onboard_safety import FakeArm, FakeHand, armed_commander  # noqa: E402


@pytest.fixture
def fake_sdk(monkeypatch):
    """The vendored controller and hand sender import unitree_sdk2py at module
    level, which no laptop has. Same stub shape as tests/test_dex3_order.py,
    widened to what g1_arm.py imports."""
    import types

    class _Pub:
        def __init__(self, *a): self.msgs = []
        def Init(self): pass
        def Write(self, m): self.msgs.append(m)

    class _Sub:
        def __init__(self, *a): pass
        def Init(self, *a, **k): pass
        def Read(self): return None

    def mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, name, m)

    mod("unitree_sdk2py")
    mod("unitree_sdk2py.core")
    mod("unitree_sdk2py.core.channel", ChannelPublisher=_Pub, ChannelSubscriber=_Sub,
        ChannelFactoryInitialize=lambda *a, **k: None)
    mod("unitree_sdk2py.idl")
    mod("unitree_sdk2py.idl.unitree_hg")
    mod("unitree_sdk2py.idl.unitree_hg.msg")
    mod("unitree_sdk2py.idl.unitree_hg.msg.dds_", HandCmd_=object, HandState_=object,
        LowCmd_=object, LowState_=object)
    mod("unitree_sdk2py.idl.unitree_go")
    mod("unitree_sdk2py.idl.unitree_go.msg")
    mod("unitree_sdk2py.idl.unitree_go.msg.dds_", LowCmd_=object, LowState_=object)
    mod("unitree_sdk2py.idl.default", unitree_hg_msg_dds__HandCmd_=lambda: None,
        unitree_hg_msg_dds__LowCmd_=lambda: None, unitree_go_msg_dds__LowCmd_=lambda: None)
    mod("unitree_sdk2py.utils")
    mod("unitree_sdk2py.utils.crc", CRC=object)
    for name in ("g1_arm", "g1_dex3"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    yield


# ---- the bridge dial ----------------------------------------------------------

def _ctrl(side, x, y=0.0):
    return {"hand": side, "source": "controller", "wrist": [0.2, -0.2, 0.0],
            "quat": [0.0, 0.0, 0.0, 1.0], "landmarks": None, "fabricated": 0,
            "trigger": 0.0, "squeeze": 0.0, "thumbstick": [x, y],
            "buttons": {"primary": False, "secondary": False, "stick": False}}


def _hand(side):
    return {"hand": side, "source": "hand", "landmarks": [0.0] * 75,
            "wrist": [0.2, -0.2, 0.0], "quat": [0.0, 0.0, 0.0, 1.0], "fabricated": 0}


def _frame(*hands):
    return {"hands": list(hands), "head": None}


def test_stick_right_turns_the_robot_to_its_right_at_the_rate():
    """x=+1 for 0.1 s at 0.5 rad/s -> -0.05 rad: robot yaw is +z up with x
    forward and y left, so the robot's right is NEGATIVE yaw."""
    d = waist.WaistDial(rate_rad_s=0.5, cap_deg=60.0)
    t = 100.0
    assert d.step(_frame(_ctrl("right", 1.0)), armed=True, measured=None, now=t) == 0.0
    out = d.step(_frame(_ctrl("right", 1.0)), armed=True, measured=None, now=t + 0.1)
    assert out == pytest.approx(-0.05, abs=1e-9)
    out = d.step(_frame(_ctrl("right", -1.0)), armed=True, measured=None, now=t + 0.2)
    assert out == pytest.approx(0.0, abs=1e-9), "stick left turns it back"


def test_the_dead_zone_holds_and_a_released_stick_holds():
    d = waist.WaistDial(rate_rad_s=0.5, cap_deg=60.0)
    d.step(_frame(_ctrl("right", 1.0)), armed=True, measured=None, now=0.0)
    d.step(_frame(_ctrl("right", 1.0)), armed=True, measured=None, now=0.1)
    held = d.step(_frame(_ctrl("right", 0.1)), armed=True, measured=None, now=0.2)
    assert held == pytest.approx(-0.05, abs=1e-9), "0.1 is inside the dead zone"
    held = d.step(_frame(_ctrl("right", 0.0)), armed=True, measured=None, now=0.3)
    assert held == pytest.approx(-0.05, abs=1e-9), "released: hold, never drift"


def test_a_held_stick_keeps_turning_when_measured_telemetry_repeats():
    """Live regression after the measured-lead clamp: motor 12 twitched in the
    requested direction, then stopped while the stick remained held. Repeated
    body samples must not pin the operator's rate-controlled target."""
    d = waist.WaistDial(rate_rad_s=0.5, cap_deg=143.0)
    frame = _frame(_ctrl("right", 1.0))
    d.step(frame, armed=True, measured=0.0, now=0.0)
    for i in range(1, 46):
        target = d.step(frame, armed=True, measured=0.0, now=i / 90)
    assert target < -0.20, "full stick for 0.5 s must keep integrating past 3 degrees"


def test_a_fresh_advancing_ack_bounds_backlog_without_pinning_the_turn():
    d = waist.WaistDial(rate_rad_s=0.5, cap_deg=143.0,
                        ack_lead_deg=3.0, ack_max_age_s=0.25)
    frame = _frame(_ctrl("right", 1.0))
    d.step(frame, armed=True, measured=0.0, now=0.0)
    for i in range(1, 46):
        ack = -0.4 * (i / 90)
        target = d.step(frame, armed=True, measured=0.0, now=i / 90,
                        acknowledged=ack, ack_age_s=0.02)
        assert abs(target - ack) <= math.radians(3.0) + 1e-9
    assert target < -0.20, "a fresh moving ack must let a held stick keep turning"


def test_a_stale_ack_falls_back_to_the_working_rate_integrator():
    d = waist.WaistDial(rate_rad_s=0.5, cap_deg=143.0,
                        ack_lead_deg=3.0, ack_max_age_s=0.25)
    frame = _frame(_ctrl("right", 1.0))
    d.step(frame, armed=True, measured=0.0, now=0.0)
    for i in range(1, 46):
        target = d.step(frame, armed=True, measured=0.0, now=i / 90,
                        acknowledged=0.0, ack_age_s=0.251)
    assert target < -0.20, "stale telemetry must never recreate twitch-and-stop"


def test_release_holds_a_fresh_ack_but_not_a_stale_one():
    moving = _frame(_ctrl("right", 1.0))
    released = _frame(_ctrl("right", 0.0))

    fresh = waist.WaistDial(rate_rad_s=0.5, cap_deg=143.0)
    fresh.step(moving, armed=True, measured=0.0, now=0.0)
    before = fresh.step(moving, armed=True, measured=0.0, now=0.1,
                        acknowledged=-0.02, ack_age_s=0.01)
    assert before < -0.02
    assert fresh.step(released, armed=True, measured=0.0, now=0.2,
                      acknowledged=-0.03, ack_age_s=0.01) == pytest.approx(-0.03)

    stale = waist.WaistDial(rate_rad_s=0.5, cap_deg=143.0)
    stale.step(moving, armed=True, measured=0.0, now=0.0)
    before = stale.step(moving, armed=True, measured=0.0, now=0.1,
                        acknowledged=-0.02, ack_age_s=1.0)
    assert stale.step(released, armed=True, measured=0.0, now=0.2,
                      acknowledged=-0.03, ack_age_s=1.0) == pytest.approx(before)


def test_the_cap_clamps_and_a_long_gap_cannot_license_a_long_step():
    d = waist.WaistDial(rate_rad_s=0.5, cap_deg=10.0)
    d.step(_frame(_ctrl("right", 1.0)), armed=True, measured=None, now=0.0)
    for i in range(1, 41):                       # 4 s of full deflection = 2 rad asked
        out = d.step(_frame(_ctrl("right", 1.0)), armed=True, measured=None, now=0.1 * i)
    assert out == pytest.approx(-math.radians(10.0)), "capped, not -2 rad"
    d2 = waist.WaistDial(rate_rad_s=0.5, cap_deg=60.0)
    d2.step(_frame(_ctrl("right", 1.0)), armed=True, measured=None, now=0.0)
    out = d2.step(_frame(_ctrl("right", 1.0)), armed=True, measured=None, now=3.0)
    assert abs(out) <= 0.5 * waist.MAX_DT_S + 1e-9, "dt is clamped like the onboard slew"


def test_starts_from_the_measured_waist_and_resets_when_not_armed():
    d = waist.WaistDial(rate_rad_s=0.5, cap_deg=60.0)
    assert d.step(_frame(_ctrl("right", 0.0)), armed=False, measured=0.3, now=0.0) is None
    first = d.step(_frame(_ctrl("right", 0.0)), armed=True, measured=0.3, now=1.0)
    assert first == pytest.approx(0.3), "the first ARMED frame starts where the waist is"
    d.step(_frame(_ctrl("right", 1.0)), armed=True, measured=0.3, now=1.1)
    moved = d.step(_frame(_ctrl("right", 1.0)), armed=True, measured=0.3, now=1.2)
    assert moved == pytest.approx(0.3 - 0.10), "two 0.1 s frames at full deflection"
    assert d.step(_frame(_ctrl("right", 1.0)), armed=False, measured=0.3, now=1.3) is None
    again = d.step(_frame(_ctrl("right", 0.0)), armed=True, measured=0.1, now=2.0)
    assert again == pytest.approx(0.1), "re-arming re-captures; nothing carried over"


def test_no_measurement_means_start_at_zero_and_say_so(capsys):
    d = waist.WaistDial(rate_rad_s=0.5, cap_deg=60.0)
    assert d.step(_frame(_ctrl("right", 0.0)), armed=True, measured=None, now=0.0) == 0.0
    assert "no measured waist" in capsys.readouterr().out


def test_a_tracked_hand_or_the_left_stick_never_moves_the_waist():
    d = waist.WaistDial(rate_rad_s=0.5, cap_deg=60.0)
    assert d.step(_frame(_hand("right")), armed=True, measured=None, now=0.0) is None
    assert d.step(_frame(_ctrl("left", 1.0)), armed=True, measured=None, now=0.1) is None
    assert d.step(_frame(), armed=True, measured=None, now=0.2) is None


def test_from_env_reads_rate_cap_and_ack_limits(monkeypatch):
    monkeypatch.setenv("TELEOP_WAIST_RATE_RAD_S", "0.25")
    monkeypatch.setenv("TELEOP_WAIST_MAX_DEG", "45")
    monkeypatch.setenv("TELEOP_WAIST_ACK_LEAD_DEG", "2")
    monkeypatch.setenv("TELEOP_WAIST_ACK_MAX_AGE_S", "0.4")
    d = waist.WaistDial.from_env()
    assert d.rate == 0.25 and d.cap == pytest.approx(math.radians(45))
    assert d.ack_lead == pytest.approx(math.radians(2))
    assert d.ack_max_age_s == 0.4
    monkeypatch.setenv("TELEOP_WAIST_RATE_RAD_S", "nan")
    with pytest.raises(ValueError):
        waist.WaistDial.from_env()


# ---- into the targets, and onto the wire ----------------------------------------

class _Sink:
    onboard_state = "ARMED"
    onboard_body = {"idx": list(range(12, 29)), "q": [0.2] + [0.0] * 16}
    last_waist = {"cmd": 0.18, "q": 0.2, "clamped": False, "leashed": False}
    last_waist_t = 9.95


def test_the_target_appears_as_waist_yaw_joint_only_while_armed():
    app = {"sink": _Sink(), "waist": waist.WaistDial(rate_rad_s=0.5, cap_deg=60.0)}
    stats = {}
    targets = {"right_elbow_joint": 0.1}
    server.waist_on_thumbstick(_frame(_ctrl("right", 0.0)), stats, app, targets, now=0.0)
    assert targets["waist_yaw_joint"] == pytest.approx(0.2), "seeded from body q[0] (motor 12)"
    app["sink"].onboard_state = "HOLDING"
    targets = {"right_elbow_joint": 0.1}
    server.waist_on_thumbstick(_frame(_ctrl("right", 1.0)), stats, app, targets, now=0.1)
    assert "waist_yaw_joint" not in targets
    assert "right_elbow_joint" in targets, "the arm targets are untouched"


def test_the_bridge_passes_the_fresh_onboard_waist_ack_to_the_dial():
    class Spy:
        def step(self, frame, armed, measured, now, acknowledged=None, ack_age_s=None):
            assert acknowledged == pytest.approx(0.18)
            assert ack_age_s == pytest.approx(0.05)
            return measured

    app = {"sink": _Sink(), "waist": Spy()}
    targets = {}
    server.waist_on_thumbstick(_frame(_ctrl("right", 1.0)), {}, app, targets, now=10.0)
    assert targets["waist_yaw_joint"] == pytest.approx(0.2)


def test_the_bridge_queues_stick_velocity_for_the_independent_waist_path():
    class VelocitySink(_Sink):
        def __init__(self):
            self.velocities = []

        def set_waist_velocity(self, value):
            self.velocities.append(value)

    sink = VelocitySink()
    app = {"sink": sink, "waist": waist.WaistDial(rate_rad_s=0.5, cap_deg=60.0)}
    targets = {}
    server.waist_on_thumbstick(_frame(_ctrl("right", 1.0)), {}, app, targets, now=0.0)
    assert sink.velocities == pytest.approx([-0.5])
    assert targets["waist_yaw_joint"] == pytest.approx(0.2), (
        "the orange/operator-intent target remains available for recording")


def test_without_a_dial_the_targets_are_untouched():
    targets = {"right_elbow_joint": 0.1}
    server.waist_on_thumbstick(_frame(_ctrl("right", 1.0)), {}, {"sink": _Sink()}, targets, now=0.0)
    assert targets == {"right_elbow_joint": 0.1}


# ---- the operator's waist lock (laptop control page) ---------------------------------

class _VelocitySink(_Sink):
    def __init__(self):
        self.velocities = []

    def set_waist_velocity(self, value):
        self.velocities.append(value)


def test_a_locked_waist_ignores_a_full_stick_and_sends_zero_velocity():
    """Locked, a full right stick must not turn the waist: velocity 0 goes to
    the onboard (so it stops), and the target stays where it was seeded."""
    sink = _VelocitySink()
    app = {"sink": sink, "waist": waist.WaistDial(rate_rad_s=0.5, cap_deg=60.0),
           "waist_frozen": True}
    for i in range(5):
        targets = {}
        server.waist_on_thumbstick(_frame(_ctrl("right", 1.0)), {}, app, targets, now=i * 0.05)
        assert targets["waist_yaw_joint"] == pytest.approx(0.2)
    assert sink.velocities and all(v == 0.0 for v in sink.velocities)


def test_locking_mid_turn_stops_the_turn():
    """The lock applied while the stick is held: the next frame sends 0, not
    the last nonzero velocity, and the target snaps to the onboard's ack."""
    sink = _VelocitySink()
    app = {"sink": sink, "waist": waist.WaistDial(rate_rad_s=0.5, cap_deg=60.0)}
    server.waist_on_thumbstick(_frame(_ctrl("right", 1.0)), {}, app, {}, now=10.0)
    server.waist_on_thumbstick(_frame(_ctrl("right", 1.0)), {}, app, {}, now=10.05)
    assert sink.velocities[-1] == pytest.approx(-0.5)
    app["waist_frozen"] = True
    targets = {}
    server.waist_on_thumbstick(_frame(_ctrl("right", 1.0)), {}, app, targets, now=10.1)
    assert sink.velocities[-1] == 0.0
    assert targets["waist_yaw_joint"] == pytest.approx(_Sink.last_waist["cmd"])


def test_unlocking_gives_the_stick_back():
    sink = _VelocitySink()
    app = {"sink": sink, "waist": waist.WaistDial(rate_rad_s=0.5, cap_deg=60.0),
           "waist_frozen": True}
    server.waist_on_thumbstick(_frame(_ctrl("right", 1.0)), {}, app, {}, now=0.0)
    app["waist_frozen"] = False
    server.waist_on_thumbstick(_frame(_ctrl("right", 1.0)), {}, app, {}, now=0.05)
    assert sink.velocities == pytest.approx([0.0, -0.5])


def test_the_lock_does_not_rewrite_what_the_operator_did():
    """The recorded frame must still say where the thumb was."""
    f = _frame(_ctrl("right", 0.8, 0.3), _ctrl("left", -0.4))
    c = server.stick_centred(f)
    assert f["hands"][0]["thumbstick"] == [0.8, 0.3]
    assert c["hands"][0]["thumbstick"] == [0.0, 0.3]
    assert c["hands"][1]["thumbstick"] == [-0.4, 0.0], "the left stick is not the waist"


def _lock_req(app, method, query=None):
    import types
    return types.SimpleNamespace(app=app, method=method, query=dict(query or {}), headers={})


def test_the_lock_endpoint_needs_the_key_and_reports_the_setting():
    app = {"control_key": "k", "waist_frozen": False}
    r = asyncio.run(server.waist_lock_handler(_lock_req(app, "POST", {"frozen": "1"})))
    assert r.status == 403 and app["waist_frozen"] is False
    r = asyncio.run(server.waist_lock_handler(_lock_req(app, "POST", {"frozen": "1", "key": "k"})))
    assert r.status == 200 and app["waist_frozen"] is True
    r = asyncio.run(server.waist_lock_handler(_lock_req(app, "GET")))
    assert b'"frozen": true' in r.body
    r = asyncio.run(server.waist_lock_handler(_lock_req(app, "POST", {"frozen": "maybe", "key": "k"})))
    assert r.status == 400 and app["waist_frozen"] is True


def test_the_status_and_the_control_page_carry_the_lock():
    src = Path(server.__file__).read_text()
    handler = src.split("async def control_status_handler")[1].split("\nasync def ")[0]
    assert '"waist_frozen"' in handler
    page = (Path(server.__file__).parent.parent / "web" / "control.html").read_text()
    assert 'id="waist-lock"' in page and "showWaistLock(s.waist_frozen)" in page
    assert 'fetch("/waist?"' in page


def _sink():
    s = RealG1Sink.__new__(RealG1Sink)
    import threading
    s._lock = threading.Lock()
    s._latest = None
    s._last_hands = None
    s.n_retargeted = 0
    s.onboard_state = "HOLDING"
    s.onboard_q = None
    return s


def _full_targets(waist_yaw=None):
    from retarget_g1 import ARM_LEFT, ARM_RIGHT, DEX3_LEFT, DEX3_RIGHT
    t = {n: 0.1 for n in ARM_LEFT + ARM_RIGHT + DEX3_LEFT + DEX3_RIGHT}
    if waist_yaw is not None:
        t["waist_yaw_joint"] = waist_yaw
    return t


def test_the_sink_carries_the_waist_beside_the_unchanged_14_vector():
    s = _sink()
    s.set_waist_velocity(-0.5)
    asyncio.run(s.apply(_full_targets(waist_yaw=-0.3)))
    latest = s.fresh_frame()
    assert len(latest["arm"]) == 14, "the 14-vector keeps its meaning"
    assert latest["waist"] == pytest.approx(-0.3)
    assert latest["waist_detached"] is True
    assert s.fresh_waist() == {
        "waist_velocity_only": True,
        "waist_velocity": pytest.approx(-0.5),
    }
    assert s.commanded()["waist_yaw_joint"] == pytest.approx(-0.3), "so `sent` records it"
    s2 = _sink()
    asyncio.run(s2.apply(_full_targets()))
    assert "waist" not in s2.fresh_frame(), "no waist commanded: no waist on the wire"


def test_a_waist_alone_is_not_a_frame():
    s = _sink()
    asyncio.run(s.apply({"waist_yaw_joint": 0.1}))
    assert s.fresh_frame() is None


def test_a_non_finite_waist_drops_the_frame():
    s = _sink()
    asyncio.run(s.apply(_full_targets(waist_yaw=float("nan"))))
    assert s.fresh_frame() is None


# ---- onboard -------------------------------------------------------------------

class WaistArm(FakeArm):
    """The vendored controller after this change: an optional waist kwarg."""
    def __init__(self):
        super().__init__()
        self.waists = []

    def ctrl_dual_arm(self, q, dq, waist_yaw=None):
        self.commands.append(list(q))
        self.waists.append(waist_yaw)

    def set_waist_target(self, waist_yaw):
        self.waists.append(waist_yaw)

    def set_waist_velocity(self, velocity, **kwargs):
        self.waists.append((velocity, kwargs))

    def waist_control_state(self):
        return {"target": 0.2, "velocity": -0.5, "stale": False,
                "leashed": False}

    def stop_waist_velocity(self, *, hold_measured=True):
        self.waists.append(("stop", hold_measured))


def _armed_with_waist(measured_waist=0.0):
    c = armed_commander()
    c.arm = WaistArm()
    c.measured_arm_q = [0.0] * ob.ARM_DIM
    c.measured_body = {"idx": ob.BODY_IDX, "q": [measured_waist] + [0.0] * 16,
                       "dq": [0.0] * 17, "tau": None, "tick": None, "t": time.monotonic()}
    c.last_apply_t = 0.0
    return c


def test_apply_without_a_waist_never_writes_motor_12():
    c = _armed_with_waist()
    assert c.apply([0.0] * ob.ARM_DIM, [0.0] * 7, [0.0] * 7) is True
    assert c.arm.waists == [None]
    assert c.last_waist is None


def test_sanitize_waist_clamps_to_the_limit_and_refuses_nan():
    lo, hi = ob.effective_waist_limits()
    assert lo < 0 < hi and hi <= ob.WAIST_MODEL_MAX_RAD, "never past the model's stop"
    assert ob.sanitize_waist(100.0) == hi and ob.sanitize_waist(-100.0) == lo
    with pytest.raises(ValueError):
        ob.sanitize_waist(float("nan"))
    with pytest.raises(ValueError):
        ob.sanitize_waist("0.3")


def test_sanitize_waist_velocity_has_an_independent_onboard_speed_cap():
    assert ob.sanitize_waist_velocity(0.5) == pytest.approx(0.5)
    assert ob.sanitize_waist_velocity(99.0) == pytest.approx(ob.WAIST_VELOCITY_MAX_RAD_S)
    assert ob.sanitize_waist_velocity(-99.0) == pytest.approx(-ob.WAIST_VELOCITY_MAX_RAD_S)
    for invalid in (float("nan"), "0.5", True):
        with pytest.raises(ValueError):
            ob.sanitize_waist_velocity(invalid)


def test_the_default_limit_is_the_joint_s_own_travel_short_of_the_stop():
    """The operator asked for the whole turn; only the stop itself is off limits."""
    lo, hi = -ob.WAIST_MAX_RAD_DEFAULT, ob.WAIST_MAX_RAD_DEFAULT
    assert hi < ob.WAIST_MODEL_MAX_RAD, "stops short of the mechanical stop"
    assert hi > 2.0, "not the old +-1.5 placeholder"
    assert math.radians(waist.DEFAULT_CAP_DEG) <= hi + 1e-9, (
        "the stick must not ask for more than the onboard will allow")
    assert math.radians(waist.DEFAULT_CAP_DEG) > 2.0, "the stick can reach it"


def test_g1_waist_max_rad_lowers_the_limit_and_a_bad_value_does_not():
    assert ob._waist_max_from_env({"G1_WAIST_MAX_RAD": "0.5"}) == 0.5
    assert ob._waist_max_from_env({"G1_WAIST_MAX_RAD": "-0.5"}) == 0.5, "sign-blind"
    assert ob._waist_max_from_env({}) == ob.WAIST_MAX_RAD_DEFAULT
    assert ob._waist_max_from_env({"G1_WAIST_MAX_RAD": ""}) == ob.WAIST_MAX_RAD_DEFAULT
    # Never ABOVE the model, whatever the environment says.
    assert ob._waist_max_from_env({"G1_WAIST_MAX_RAD": "99"}) == ob.WAIST_MODEL_MAX_RAD
    for bad in ("banana", "nan", "0", "inf"):
        got = ob._waist_max_from_env({"G1_WAIST_MAX_RAD": bad})
        assert got == ob.WAIST_MAX_RAD_DEFAULT, bad


def test_the_waist_is_slewed_at_its_own_slower_rate():
    c = _armed_with_waist()
    c.apply([0.0] * ob.ARM_DIM, [0.0] * 7, [0.0] * 7, waist=0.0)
    c.last_apply_t = 0.0
    c._waist_last_t -= 0.1                     # 100 ms since the last waist command
    c.apply([0.0] * ob.ARM_DIM, [0.0] * 7, [0.0] * 7, waist=1.0)
    assert c.arm.waists[-1] == pytest.approx(ob.WAIST_SLEW_RAD_S * 0.1, abs=1e-6)
    assert ob.WAIST_SLEW_RAD_S < ob.ARM_SLEW_RAD_S


def test_the_waist_is_leashed_to_the_measured_motor_12():
    c = _armed_with_waist(measured_waist=0.5)
    c._waist_last_cmd = 1.0                     # command already far ahead
    c._waist_last_t = time.time()
    c.apply([0.0] * ob.ARM_DIM, [0.0] * 7, [0.0] * 7, waist=1.0)
    assert c.arm.waists[-1] == pytest.approx(0.5 + ob.MAX_TRACKING_ERROR_RAD)
    assert c.last_waist["leashed"] is True


def test_the_ack_carries_the_waist():
    c = _armed_with_waist(measured_waist=0.25)
    c.apply([0.0] * ob.ARM_DIM, [0.0] * 7, [0.0] * 7, waist=0.25)
    ack = ob.ack_message(c, 3, True, [])
    assert ack["waist"] == {"cmd": 0.25, "q": 0.25, "clamped": False, "leashed": False}


def test_not_armed_refuses_the_waist_like_the_arm():
    c = _armed_with_waist()
    c.state = "HOLDING"
    assert c.apply([0.0] * ob.ARM_DIM, [0.0] * 7, [0.0] * 7, waist=0.3) is False
    assert c.arm.waists == []


def test_independent_waist_refresh_does_not_keep_the_arm_watchdog_alive():
    c = _armed_with_waist(measured_waist=0.2)
    c.last_frame_t = 123.0
    assert c.apply_waist(0.4, seq=7) is True
    assert 0.2 <= c.arm.waists[-1] <= 0.2 + ob.WAIST_SLEW_RAD_S * 0.001 + 1e-9
    assert c.last_waist_seq == 7
    assert c.last_frame_t == 123.0
    c.state = "HOLDING"
    assert c.apply_waist(0.5, seq=8) is False
    assert c.last_waist_refusal == "not_armed"


def test_velocity_command_does_not_keep_the_arm_watchdog_alive_and_hold_stops_it():
    c = _armed_with_waist(measured_waist=0.2)
    c.last_frame_t = 123.0
    assert c.apply_waist_velocity(-0.5, seq=7) is True
    velocity, config = c.arm.waists[-1]
    assert velocity == pytest.approx(-0.5)
    assert config["limits"] == pytest.approx(ob.effective_waist_limits())
    assert config["tracking_error"] == pytest.approx(ob.MAX_TRACKING_ERROR_RAD)
    assert config["max_velocity"] == pytest.approx(ob.WAIST_VELOCITY_MAX_RAD_S)
    assert c.last_frame_t == 123.0
    with c.lock:
        c._freeze()
    assert ("stop", True) in c.arm.waists


def test_a_freeze_reseeds_the_waist_slew():
    c = _armed_with_waist()
    c.apply([0.0] * ob.ARM_DIM, [0.0] * 7, [0.0] * 7, waist=0.0)
    with c.lock:
        c._freeze()
    assert c._waist_last_cmd is None


def test_go_home_returns_the_waist_only_once_it_was_driven(fake_sdk):
    c = _armed_with_waist(measured_waist=0.4)
    c._waist_home = 0.0
    c.home_q = [0.0] * ob.ARM_DIM
    c.state = "HOLDING"
    c.go_home()
    assert all(w is None for w in c.arm.waists), "never driven: motor 12 untouched"
    c = _armed_with_waist(measured_waist=0.4)
    c._waist_home = 0.0
    c.apply([0.0] * ob.ARM_DIM, [0.0] * 7, [0.0] * 7, waist=0.4)
    c.home_q = [0.0] * ob.ARM_DIM
    c.state = "HOLDING"
    c.go_home()
    driven = [w for w in c.arm.waists if w is not None]
    assert driven and driven[-1] == pytest.approx(0.0, abs=ob.HOME_SLEW_RAD_S * 0.02 + 1e-6)


# ---- the vendored controller writes motor 12 only when asked ----------------------

def test_ctrl_dual_arm_writes_motor_12_only_with_a_waist(fake_sdk):
    import g1_arm
    ctl = g1_arm.G1_29_ArmController.__new__(g1_arm.G1_29_ArmController)
    import threading
    ctl.ctrl_lock = threading.Lock()
    ctl.q_target = np.zeros(14)
    ctl.tauff_target = np.zeros(14)
    ctl.waist_yaw_target = None
    ctl.ctrl_dual_arm(np.zeros(14), np.zeros(14))
    assert ctl.waist_yaw_target is None
    ctl.ctrl_dual_arm(np.zeros(14), np.zeros(14), waist_yaw=0.3)
    assert ctl.waist_yaw_target == pytest.approx(0.3)
    ctl.ctrl_dual_arm(np.zeros(14), np.zeros(14))
    assert ctl.waist_yaw_target == pytest.approx(0.3), "None means 'leave it', not 'reset'"
    before = ctl.q_target
    ctl.set_waist_target(-0.2)
    assert ctl.waist_yaw_target == pytest.approx(-0.2)
    assert ctl.q_target is before, "waist refresh must not touch the arm target"

    class _Cmd:
        def __init__(self):
            self.q = 9.0
            self.dq = 9.0
            self.tau = 9.0
    msg = type("Msg", (), {})()
    msg.motor_cmd = [_Cmd() for _ in range(35)]
    g1_arm.write_waist_cmd(msg, None)
    assert msg.motor_cmd[12].q == 9.0
    g1_arm.write_waist_cmd(msg, 0.3)
    assert msg.motor_cmd[12].q == pytest.approx(0.3) and msg.motor_cmd[12].dq == 0.0
    assert msg.motor_cmd[13].q == 9.0 and msg.motor_cmd[14].q == 9.0, "roll/pitch never"


def test_waist_velocity_advances_once_per_motor_tick_not_per_message(fake_sdk):
    import g1_arm
    import threading

    ctl = g1_arm.G1_29_ArmController.__new__(g1_arm.G1_29_ArmController)
    ctl.ctrl_lock = threading.Lock()
    ctl.control_dt = 0.02
    ctl.waist_yaw_target = 0.0
    ctl.waist_velocity_rad_s = 0.0
    ctl.waist_velocity_t = 0.0
    ctl.waist_tick_t = 10.0
    ctl.waist_velocity_timeout_s = 0.15
    ctl.waist_velocity_limits = (-1.0, 1.0)
    ctl.waist_tracking_error = 0.3

    for _ in range(100):
        ctl.set_waist_velocity(-0.5, now=10.0, limits=(-1.0, 1.0),
                               tracking_error=0.3, timeout_s=0.15)
    assert ctl.waist_yaw_target == 0.0, "messages update velocity, never position"
    assert ctl._advance_waist_velocity(measured=0.0, now=10.02) == pytest.approx(-0.01)
    assert ctl._advance_waist_velocity(measured=-0.01, now=10.04) == pytest.approx(-0.02)


def test_fifty_and_one_hundred_hz_packets_produce_the_same_motor_trajectory(fake_sdk):
    import g1_arm
    import threading

    def replay(messages_per_tick):
        ctl = g1_arm.G1_29_ArmController.__new__(g1_arm.G1_29_ArmController)
        ctl.ctrl_lock = threading.Lock()
        ctl.control_dt = 0.02
        ctl.waist_yaw_target = 0.0
        ctl.waist_velocity_rad_s = 0.0
        ctl.waist_velocity_t = 0.0
        ctl.waist_tick_t = 0.0
        ctl.waist_velocity_timeout_s = 0.15
        ctl.waist_velocity_limits = (-2.5, 2.5)
        ctl.waist_tracking_error = 0.3
        out = []
        measured = 0.0
        for tick in range(1, 51):
            now = tick * 0.02
            for message in range(messages_per_tick):
                stamp = now - (messages_per_tick - message - 1) * 0.01
                ctl.set_waist_velocity(-0.5, now=stamp, limits=(-2.5, 2.5),
                                       tracking_error=0.3, timeout_s=0.15)
            measured = ctl._advance_waist_velocity(measured=measured, now=now)
            out.append(measured)
        return out

    assert replay(1) == pytest.approx(replay(2), abs=1e-12)


def test_stale_waist_velocity_holds_the_last_motor_tick_target(fake_sdk):
    import g1_arm
    import threading

    ctl = g1_arm.G1_29_ArmController.__new__(g1_arm.G1_29_ArmController)
    ctl.ctrl_lock = threading.Lock()
    ctl.control_dt = 0.02
    ctl.waist_yaw_target = 0.2
    ctl.waist_velocity_rad_s = 0.5
    ctl.waist_velocity_t = 20.0
    ctl.waist_tick_t = 20.0
    ctl.waist_velocity_timeout_s = 0.15
    ctl.waist_velocity_limits = (-1.0, 1.0)
    ctl.waist_tracking_error = 0.3
    assert ctl._advance_waist_velocity(measured=0.2, now=20.02) == pytest.approx(0.21)
    held = ctl._advance_waist_velocity(measured=0.21, now=20.20)
    assert held == pytest.approx(0.21)
    assert ctl.waist_control_state()["stale"] is True


def test_low_level_velocity_setter_clamps_even_if_a_caller_bypasses_protocol(fake_sdk):
    import g1_arm
    import threading

    ctl = g1_arm.G1_29_ArmController.__new__(g1_arm.G1_29_ArmController)
    ctl.ctrl_lock = threading.Lock()
    ctl.waist_yaw_target = 0.0
    ctl.waist_velocity_rad_s = 0.0
    ctl.waist_tick_t = 1.0
    ctl.set_waist_velocity(99.0, now=1.0, limits=(-1.0, 1.0),
                           tracking_error=0.3, timeout_s=0.15, max_velocity=0.6)
    assert ctl.waist_velocity_rad_s == pytest.approx(0.6)
