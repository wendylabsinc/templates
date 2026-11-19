"""Exercise the real bridge and authenticated onboard websocket boundaries."""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import g1_onboard as onboard
import server
from g1_locomotion import Locomotion
from locomotion import left_stick_velocity
from sink_realg1 import RealG1Sink


def sink():
    value = RealG1Sink.__new__(RealG1Sink)
    value._lock = threading.Lock()
    value.onboard_state = "ARMED"
    return value


@pytest.mark.parametrize("source,state", [("jog", "ARMED"), ("xr", "HOLDING")])
def test_non_xr_or_unarmed_source_only_queues_stop(source, state):
    target = sink()
    target.onboard_state = state
    server.locomotion_on_thumbstick({"hands": []}, {"sink": target, "source": source})
    assert target.pop_locomotion()["locomotion"]["active"] is False


def test_sink_consumes_once_and_does_not_refresh_stale_input(monkeypatch):
    target = sink()
    monkeypatch.setattr(time, "monotonic", lambda: 10.)
    target.set_locomotion({"active": True, "vx": .25, "yaw": 0.})
    assert target.pop_locomotion()["locomotion"]["vx"] == .25
    assert target.pop_locomotion() is None
    target.set_locomotion({"active": True, "vx": .25, "yaw": 0.})
    monkeypatch.setattr(time, "monotonic", lambda: 11.)
    assert target.pop_locomotion()["locomotion"]["active"] is False


def test_real_xr_websocket_reaches_locomotion_sink():
    class Sink(server.LogSink):
        onboard_state = "ARMED"
        def set_locomotion(self, value):
            self.intent = value

    async def run():
        target = Sink()
        app = server.make_app(server.DebugRetargeter(), target)
        permission = server.walking_permission(app)
        permission.set_allowed(True)
        permission.filter({"active": True, "vx": 0., "yaw": 0.})
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/teleop") as ws:
                await ws.send_json({"hands": [{
                    "hand": "left", "source": "controller", "thumbstick": [1, -1],
                    "landmarks": [0.] * 75, "wrist": [0., 0., 0.],
                    "quat": [0., 0., 0., 1.], "fabricated": 0,
                }]})
                for _ in range(100):
                    if hasattr(target, "intent"):
                        break
                    await asyncio.sleep(.005)
                assert target.intent == {"active": True, "vx": .25, "yaw": -.4}
    asyncio.run(run())


def test_onboard_wire_route_and_disconnect_do_not_refresh_arm_watchdog():
    class SDK:
        def __init__(self): self.calls = []
        def GetFsmId(self): return 0, 801
        def SetVelocity(self, *args): self.calls.append(args); return 0

    async def run():
        sdk = SDK()
        control = Locomotion(sdk, lambda: (True, 1))
        cmd = SimpleNamespace(lock=threading.Lock(), state="ARMED", measured_arm_q=None,
                              claim=lambda: "test-owner", reset_rx_age=lambda: None,
                              operator_disconnected=lambda token: None, locomotion=control,
                              last_frame_t=123.)
        app = web.Application()
        app["cmd"], app["secret"] = cmd, "test-key"
        app.router.add_get("/ctrl", onboard.ws_handler)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/ctrl?key=wrong") as denied:
                assert (await denied.receive_json())["error"] == "bad key"
            async with client.ws_connect("/ctrl?key=test-key") as ws:
                for intent in ({"active": True, "vx": 0., "yaw": 0.},
                               {"active": True, "vx": .25, "yaw": -.4}):
                    await ws.send_json({"locomotion_only": True, "locomotion": intent})
                    # The idle stream confirms the receive loop has processed
                    # the message. No arm or hand target is needed for routing.
                    await asyncio.wait_for(ws.receive_json(), 1.)
                    control.tick()
                assert sdk.calls[-1] == (.25, 0., -.4, .2)
                assert cmd.last_frame_t == 123.
            for _ in range(100):
                if control._latest is None:
                    break
                await asyncio.sleep(.005)
            control.tick()
            assert sdk.calls[-1][:3] == (0., 0., 0.)
    asyncio.run(run())


@pytest.mark.parametrize("change", ["owner", "state", "discover", "homing",
                                    "deadman", "lowstate", "arm_age"])
def test_commander_gate_requires_existing_authority_and_fresh_feedback(change):
    cmd = onboard.Commander.__new__(onboard.Commander)
    cmd.lock = threading.RLock()
    cmd.state, cmd.owner, cmd.discover, cmd.homing = "ARMED", "owner", False, False
    cmd.n_armed = 3
    cmd.deadman_ok = lambda: True
    cmd.lowstate_ok = lambda: True
    cmd.lowstate_t = cmd.last_frame_t = time.time()
    assert cmd.locomotion_gate() == (True, 3)
    if change == "owner": cmd.owner = None
    elif change == "state": cmd.state = "HOLDING"
    elif change == "discover": cmd.discover = True
    elif change == "homing": cmd.homing = True
    elif change == "deadman": cmd.deadman_ok = lambda: False
    elif change == "lowstate": cmd.lowstate_t -= 1
    elif change == "arm_age": cmd.last_frame_t -= 1
    assert cmd.locomotion_gate() == (False, 3)
