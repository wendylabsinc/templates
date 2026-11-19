"""Left-stick walking stays bounded, leased and independent of arm commands."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))
sys.path.insert(0, str(ROOT / "onboard"))
from locomotion import left_stick_velocity
from g1_locomotion import Locomotion


def frame(x=0, y=0, **extra):
    return {"hands": [{"hand": "left", "source": "controller",
                       "thumbstick": [x, y], **extra}]}


def test_forward_backward_and_turn_signs():
    assert left_stick_velocity(frame(1, -1)) == {
        "active": True, "vx": .25, "yaw": -.4}
    assert left_stick_velocity(frame(-1, 1)) == {
        "active": True, "vx": -.25, "yaw": .4}


@pytest.mark.parametrize("sample", [
    {"hands": []}, frame(source="hand"), frame(float("nan")),
    frame(True), frame(2), frame(0, 0, buttons={"stick": True}),
])
def test_missing_bad_or_clicked_controller_cannot_move(sample):
    result = left_stick_velocity(sample)
    assert result == {"active": False, "vx": 0., "yaw": 0.}


def test_dead_zone_is_neutral():
    assert left_stick_velocity(frame(.1, -.1)) == {
        "active": True, "vx": 0., "yaw": 0.}


class Client:
    def __init__(self):
        self.calls = []
        self.code = 0
        self.fsm = 801

    def GetFsmId(self):
        return 0, self.fsm

    def SetVelocity(self, *args):
        self.calls.append(args)
        return self.code


def controller():
    client = Client()
    gate = [True, 1]
    clock = [10.]
    control = Locomotion(client, lambda: tuple(gate), clock=lambda: clock[0])
    return control, client, gate, clock


def submit(control, x=0, y=0):
    control.submit(left_stick_velocity(frame(x, y)))
    control.tick()


def test_neutral_required_after_arming_and_after_timeout():
    control, client, gate, clock = controller()
    submit(control, y=-1)
    assert client.calls == []
    submit(control)
    submit(control, y=-1)
    assert client.calls[-1] == (.25, 0., 0., .2)
    clock[0] += .3
    control.tick()
    assert client.calls[-1][:3] == (0., 0., 0.)
    submit(control, y=-1)
    assert client.calls[-1][:3] == (0., 0., 0.)
    submit(control)
    submit(control, x=1)
    assert client.calls[-1][:3] == (0., 0., -.4)


@pytest.mark.parametrize("cause", ["hold", "rearm", "fsm", "disconnect", "inactive"])
def test_stop_and_recenter_on_loss_of_authority(cause):
    control, client, gate, clock = controller()
    submit(control)
    submit(control, y=-1)
    if cause == "hold":
        gate[0] = False
    elif cause == "rearm":
        gate[1] += 1
    elif cause == "fsm":
        client.fsm = 500
    elif cause == "disconnect":
        control.reset()
    else:
        control.submit({"active": False, "vx": 0., "yaw": 0.})
    control.tick()
    assert client.calls[-1][:3] == (0., 0., 0.)


@pytest.mark.parametrize("bad", [
    {}, {"active": True, "vx": float("inf"), "yaw": 0},
    {"active": True, "vx": True, "yaw": 0},
    {"active": True, "vx": 3, "yaw": 0},
])
def test_invalid_wire_data_stops_existing_motion(bad):
    control, client, gate, clock = controller()
    submit(control)
    submit(control, y=-1)
    control.submit(bad)
    control.tick()
    assert client.calls[-1][:3] == (0., 0., 0.)


def test_rpc_failure_latches_until_neutral_and_reports_error():
    control, client, gate, clock = controller()
    submit(control)
    client.code = 42
    submit(control, y=-1)
    assert control.status()["rpc_code"] == 42
    client.code = 0
    submit(control, y=-1)
    assert client.calls[-1][:3] == (0., 0., 0.)


def test_idle_control_never_claims_locomotion_with_zero_rpcs():
    control, client, gate, clock = controller()
    submit(control)
    clock[0] += 1
    control.tick()
    assert client.calls == []


def test_controller_refresh_during_fsm_query_uses_newest_input():
    control, client, gate, clock = controller()
    submit(control)
    def query():
        control.submit(left_stick_velocity(frame(-1, 0)))
        return 0, 801
    client.GetFsmId = query
    submit(control, y=-1)
    assert client.calls[-1][:3] == (0., 0., .4)


def test_loss_of_gate_during_fsm_query_prevents_movement():
    control, client, gate, clock = controller()
    submit(control)
    def query():
        gate[0] = False
        return 0, 801
    client.GetFsmId = query
    submit(control, y=-1)
    assert client.calls == []


def test_failed_stop_retries_and_does_not_reenable_on_neutral():
    control, client, gate, clock = controller()
    submit(control)
    submit(control, y=-1)
    client.code = 42
    submit(control)
    assert control.status()["ready"] is False
    count = len(client.calls)
    client.code = 0
    submit(control, y=-1)
    assert len(client.calls) == count + 1
    assert client.calls[-1][:3] == (0., 0., 0.)
    submit(control)
    assert client.calls[-1][:3] == (0., 0., 0.)
