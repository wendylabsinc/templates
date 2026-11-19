"""The laptop operator owns walking permission independently of arm control."""
import asyncio
import json
from types import SimpleNamespace

import server
from locomotion import WalkingPermission


STOP = {"active": False, "vx": 0., "yaw": 0.}
NEUTRAL = {"active": True, "vx": 0., "yaw": 0.}
MOVE = {"active": True, "vx": .25, "yaw": -.4}


def test_walking_is_blocked_on_start_and_a_new_bridge_does_not_restore_allow():
    permission = WalkingPermission()
    assert permission.filter(MOVE) == STOP
    permission.set_allowed(True)
    assert WalkingPermission().filter(MOVE) == STOP


def test_allowing_requires_real_neutral_input_then_passes_movement():
    permission = WalkingPermission()
    permission.set_allowed(True)
    assert permission.filter(MOVE) == STOP
    assert permission.filter(STOP) == STOP  # missing controller is not neutral
    assert permission.filter(NEUTRAL) == NEUTRAL
    assert permission.filter(MOVE) == MOVE


def test_quick_block_allow_cannot_resume_a_held_stick():
    permission = WalkingPermission()
    permission.set_allowed(True)
    permission.filter(NEUTRAL)
    assert permission.filter(MOVE) == MOVE
    permission.set_allowed(False)
    permission.set_allowed(True)
    assert permission.filter(MOVE) == STOP


class Sink:
    onboard_state = "ARMED"
    def __init__(self): self.intents = []
    def set_locomotion(self, intent): self.intents.append(intent)


def request(app, method="POST", **query):
    return SimpleNamespace(app=app, method=method, query=query, headers={})


def test_block_queues_stop_without_waiting_for_another_headset_frame():
    sink = Sink()
    app = {"sink": sink, "control_key": "secret", "locomotion_permission": WalkingPermission()}
    app["locomotion_permission"].set_allowed(True)
    response = asyncio.run(server.locomotion_permission_handler(
        request(app, allowed="0", key="secret")))
    assert response.status == 200
    assert sink.intents == [STOP]
    assert json.loads(response.body)["allowed"] is False
    assert sink.onboard_state == "ARMED"  # never freezes/releases the arms


def test_permission_mutation_uses_control_key_and_get_is_read_only():
    permission = WalkingPermission()
    app = {"sink": Sink(), "control_key": "secret", "locomotion_permission": permission}
    for req, status in [(request(app, allowed="1"), 403),
                        (request(app, key="secret", allowed="maybe"), 400),
                        (request(app, "GET", allowed="1"), 200)]:
        response = asyncio.run(server.locomotion_permission_handler(req))
        assert response.status == status
        assert permission.status()["allowed"] is False
    response = asyncio.run(server.locomotion_permission_handler(
        request(app, key="secret", allowed="1")))
    assert json.loads(response.body)["awaiting_center"] is True
    assert app["sink"].intents == [STOP]  # enabling never queues motion


def test_frame_path_obeys_permission_and_resets_on_other_sources():
    sink = Sink()
    permission = WalkingPermission()
    app = {"sink": sink, "source": "xr", "locomotion_permission": permission}
    frame = {"hands": [{"hand": "left", "source": "controller", "thumbstick": [0, -1]}]}
    server.locomotion_on_thumbstick(frame, app)
    assert sink.intents[-1] == STOP
    permission.set_allowed(True)
    permission.filter(NEUTRAL)
    server.locomotion_on_thumbstick(frame, app)
    assert sink.intents[-1]["vx"] == .25
    app["source"] = "jog"
    server.locomotion_on_thumbstick(frame, app)
    assert sink.intents[-1] == STOP
    app["source"] = "xr"
    server.locomotion_on_thumbstick(frame, app)
    assert sink.intents[-1] == STOP


def test_operator_block_stops_the_existing_onboard_velocity_path():
    from g1_locomotion import Locomotion
    class SDK:
        def __init__(self): self.calls = []
        def GetFsmId(self): return 0, 801
        def SetVelocity(self, *args): self.calls.append(args); return 0
    sdk = SDK()
    onboard = Locomotion(sdk, lambda: (True, 1))
    class WireSink(Sink):
        def set_locomotion(self, intent):
            super().set_locomotion(intent)
            onboard.submit(intent)
    sink = WireSink()
    permission = WalkingPermission()
    app = {"sink": sink, "source": "xr", "locomotion_permission": permission}
    permission.set_allowed(True)
    for axes in ([0, 0], [0, -1]):
        server.locomotion_on_thumbstick({"hands": [{"hand": "left", "source": "controller", "thumbstick": axes}]}, app)
        onboard.tick()
    assert sdk.calls[-1][:3] == (.25, 0., 0.)
    asyncio.run(server.locomotion_permission_handler(request(app, allowed="0")))
    onboard.tick()
    assert sdk.calls[-1][:3] == (0., 0., 0.)
