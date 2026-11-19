"""Operator-requested return home on input loss, with no physical hardware."""
import sys
import asyncio
from pathlib import Path
import threading
import time

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "onboard"))
import g1_onboard as onboard


class Arm:
    def __init__(self, cmd):
        self.cmd = cmd
        self.q = np.full(14, .2)
        self.sent = []

    def get_current_dual_arm_q(self):
        return self.q.copy()

    def ctrl_dual_arm(self, q, tau):
        self.q = np.array(q)
        self.sent.append(self.q.copy())
        self.cmd.measured_arm_q = self.q.tolist()


@pytest.fixture
def cmd(monkeypatch):
    c = onboard.Commander.__new__(onboard.Commander)
    c.lock = threading.RLock()
    c.hand = None
    c.hands_only = False
    c.state = "ARMED"
    c.home_q = [.4] * 14
    c.homing = False
    c.arm = Arm(c)
    c.measured_arm_q = c.arm.q.tolist()
    c.lowstate_ok = lambda: True
    c.deadman_ok = lambda: True
    c._restore_gains = lambda: None
    c._stop = False
    c.n_watchdog = 0
    c.last_frame_t = time.time() - 10
    c.last_rx_t = time.time()
    c.n_armed = 0
    monkeypatch.setattr(onboard.time, "sleep", lambda _: None)
    return c


def test_input_loss_slews_to_saved_home_without_rearming(cmd):
    reply = cmd.recover_input_loss("headset tracking lost")
    assert "at home" in reply
    assert cmd.state == "HOLDING" and not cmd.homing
    assert np.allclose(cmd.arm.q, cmd.home_q)
    steps = np.diff(np.array(cmd.arm.sent), axis=0)
    assert len(steps) > 5
    assert np.max(np.abs(steps)) <= onboard.HOME_SLEW_RAD_S * .02 + 1e-9
    assert cmd.input_loss_recovery["reason"] == "headset tracking lost"


@pytest.mark.parametrize("why", ["feedback", "deadman", "no_home"])
def test_unusable_feedback_or_stop_never_starts_home(cmd, why):
    if why == "feedback": cmd.lowstate_ok = lambda: False
    if why == "deadman": cmd.deadman_ok = lambda: False
    if why == "no_home": cmd.home_q = None
    assert "refused" in cmd.recover_input_loss(why)
    assert cmd.state == "HOLDING"
    assert all(np.allclose(q, .2) for q in cmd.arm.sent)


def test_feedback_loss_during_return_aborts_further_travel(cmd, monkeypatch):
    def lose_feedback(_): cmd.lowstate_ok = lambda: False
    monkeypatch.setattr(onboard.time, "sleep", lose_feedback)
    assert "cancelled" in cmd.recover_input_loss("USB disconnected")
    assert not cmd.homing and cmd.state == "HOLDING"
    assert max(q[0] for q in cmd.arm.sent) <= .212 + 1e-9


def test_explicit_hold_cancels_automatic_return(cmd, monkeypatch):
    def hold(_): cmd.hold()
    monkeypatch.setattr(onboard.time, "sleep", hold)
    assert "cancelled" in cmd.recover_input_loss("USB disconnected")
    assert not cmd.homing
    assert max(q[0] for q in cmd.arm.sent) <= .212 + 1e-9


def test_arm_is_refused_during_homing_even_with_fresh_tracking(cmd):
    cmd.state = "HOLDING"; cmd.homing = True
    assert "homing" in cmd.arm_on()
    assert cmd.state == "HOLDING"


def test_watchdog_uses_home_recovery(cmd, monkeypatch):
    called = []
    def recover(reason):
        called.append(reason); cmd._stop = True
    cmd.recover_input_loss = recover
    cmd._watchdog()
    assert len(called) == 1 and "liveness" in called[0]


def test_ethernet_silence_watchdog_executes_return_without_socket(cmd, monkeypatch):
    def tick(_):
        if (getattr(cmd, 'input_loss_recovery', None) or {}).get('phase') == 'complete':
            cmd._stop = True
    monkeypatch.setattr(onboard.time, 'sleep', tick)
    cmd._watchdog()
    assert cmd.n_watchdog == 1
    assert np.allclose(cmd.arm.q, cmd.home_q)
    assert cmd.state == 'HOLDING' and cmd.n_armed == 0


def test_commanded_only_home_is_not_reported_as_completed_recovery(cmd, monkeypatch):
    monkeypatch.setattr(cmd, 'go_home', lambda **kwargs: 'at home (commanded; no measured joints to confirm it)')
    cmd.recover_input_loss('Ethernet lost')
    assert cmd.input_loss_recovery['phase'] == 'incomplete'


def test_deployed_policy_is_explicitly_discoverable():
    policy = onboard.input_loss_policy()
    assert policy['mode'] == 'return_home'
    assert policy['timeout_s'] == onboard.DEAD_MAN_S == 1.0
    assert policy['home_slew_rad_s'] == onboard.HOME_SLEW_RAD_S
    assert policy['automatic_rearm'] is False


def test_manual_hold_is_not_overridden_by_later_disconnect(cmd):
    cmd.state = "HOLDING"
    cmd.recover_input_loss("operator disconnected")
    assert not cmd.arm.sent


def test_only_controlling_socket_loss_returns_home(cmd):
    cmd.owner = "operator"
    assert "not the controlling" in cmd.operator_disconnected("observer")
    assert not cmd.arm.sent and cmd.state == "ARMED"
    assert "at home" in cmd.operator_disconnected("operator")
    assert cmd.owner is None and cmd.state == "HOLDING"


def test_remote_stop_during_return_cancels(cmd, monkeypatch):
    monkeypatch.setattr(onboard.time, "sleep", lambda _: setattr(cmd, "deadman_ok", lambda: False))
    assert "cancelled" in cmd.recover_input_loss("operator disconnected")
    assert max(q[0] for q in cmd.arm.sent) <= .212 + 1e-9


def test_invalid_home_never_reaches_motor(cmd):
    cmd.home_q[0] = float("nan")
    assert "invalid" in cmd.recover_input_loss("operator disconnected")
    assert all(np.all(np.isfinite(q)) and np.allclose(q, .2) for q in cmd.arm.sent)


def test_return_reservation_blocks_rearm_before_first_step(cmd):
    cmd.state = "HOLDING"
    cmd.input_loss_recovery = {"phase": "returning"}
    assert "homing" in cmd.arm_on()


def test_manual_hold_between_loss_and_home_start_wins(cmd, monkeypatch):
    go_home = cmd.go_home
    def concurrent_hold(**kwargs):
        cmd.hold()
        return go_home(**kwargs)
    monkeypatch.setattr(cmd, "go_home", concurrent_hold)
    assert "cancelled" in cmd.recover_input_loss("USB lost")
    assert all(np.allclose(q, .2) for q in cmd.arm.sent)


def test_failed_old_recovery_cannot_write_after_newer_hold(cmd, monkeypatch):
    sent_after_hold = []
    def fail_after_hold(**kwargs):
        cmd.hold()
        sent_after_hold.append(len(cmd.arm.sent))
        raise RuntimeError("old worker failed")
    monkeypatch.setattr(cmd, "go_home", fail_after_hold)
    assert "failed" in cmd.recover_input_loss("USB lost")
    assert len(cmd.arm.sent) == sent_after_hold[0]


def test_closing_real_websocket_runs_onboard_home_without_bridge(cmd):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    cmd.owner = None
    cmd.reset_rx_age = lambda: None

    async def run():
        app = web.Application()
        app["cmd"], app["secret"] = cmd, ""
        app.router.add_get("/ctrl", onboard.ws_handler)
        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ctrl")
            await ws.close()
            for _ in range(100):
                recovery = getattr(cmd, "input_loss_recovery", None) or {}
                if recovery.get("phase") == "complete":
                    break
                await asyncio.sleep(.01)
            assert recovery.get("phase") == "complete"

    asyncio.run(run())
    assert cmd.owner is None and cmd.state == "HOLDING"
    assert np.allclose(cmd.arm.q, cmd.home_q)


def test_recovered_tracking_acks_update_homing_status(cmd):
    # Fresh rejected frames suppress the idle status stream while HOLDING.
    cmd.state = "HOLDING"
    cmd.homing = True
    cmd.input_loss_recovery = {"phase": "returning"}
    assert onboard.ack_message(cmd, 1, False)["homing"] is True
    cmd.homing = False
    cmd.input_loss_recovery = {"phase": "complete"}
    ack = onboard.ack_message(cmd, 2, False)
    assert ack["homing"] is False
    assert ack["input_loss_recovery"]["phase"] == "complete"
