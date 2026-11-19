"""Real command/filter call sites with fake DDS and controlled input only."""
import asyncio
from types import SimpleNamespace

import pytest

import g1_onboard as ob
import retarget_g1 as rg
from tests.test_onboard_safety import armed_commander
from tests.test_waist_yaw import fake_sdk  # noqa: F401


def test_latest_burst_target_survives_the_apply_rate_gate(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    monkeypatch.setattr(ob, "MAX_FRAME_HZ", 10.0)  # widen the actual 5 ms race

    async def run():
        cmd = armed_commander()
        app = web.Application()
        app["cmd"], app["secret"] = cmd, ""
        app.router.add_get("/ctrl", ob.ws_handler)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/ctrl") as ws:
                async def send(seq):
                    await ws.send_json({"seq": seq, "arm": [seq * .01] * 14})
                async def ack(seq):
                    while True:
                        row = await asyncio.wait_for(ws.receive_json(), 2)
                        if row.get("ack") == seq:
                            return row
                await send(1)
                assert (await ack(1))["applied"]
                await send(2)
                await asyncio.sleep(.01)
                await send(3)
                final = await ack(3)
                assert final["applied"], final
                assert cmd.n_applied == 2  # intermediate frame coalesced
                assert cmd.n_superseded == 1
    asyncio.run(run())


def test_oneeuro_filters_the_second_frame_instead_of_passing_a_step(monkeypatch):
    retargeter = rg.G1Retargeter.__new__(rg.G1Retargeter)
    retargeter.set_smooth(mode="oneeuro", min_cutoff=1, beta=3)
    clock = [10.0]
    monkeypatch.setattr(rg, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    joint = "right_wrist_roll_joint"
    assert retargeter._smoothed({joint: 0.0})[joint] == 0
    clock[0] += 1 / 90
    value = retargeter._smoothed({joint: .35})[joint]
    assert 0 < value < .2


def test_same_timestamp_does_not_bypass_oneeuro(monkeypatch):
    retargeter = rg.G1Retargeter.__new__(rg.G1Retargeter)
    retargeter.set_smooth(mode="oneeuro")
    monkeypatch.setattr(rg, "time", SimpleNamespace(monotonic=lambda: 10.0))
    joint = "right_wrist_roll_joint"
    retargeter._smoothed({joint: .1})
    assert retargeter._smoothed({joint: .8})[joint] == .1


def test_mailbox_wait_coalesces_and_clear_prevents_pre_stop_replay():
    async def run():
        box = ob.LatestTargetMailbox()
        box.put({"seq": 1})
        gate = asyncio.Event()
        delay = lambda: 0 if gate.is_set() else .01
        pending = asyncio.create_task(box.get(delay=delay))
        await asyncio.sleep(.001)
        box.clear()
        gate.set()
        box.put({"seq": 2})
        old, latest = await asyncio.wait_for(pending, .1)
        assert latest["seq"] == 2
        assert not old
    asyncio.run(run())


def test_feedback_source_age_is_not_reset_to_arrival_time(monkeypatch):
    import sink_realg1 as sr
    from tests.test_sink_ack import bare_sink, FakeWs
    monkeypatch.setattr(sr, "time", SimpleNamespace(monotonic=lambda: 100.0))
    sink = bare_sink()
    asyncio.run(sink._read(FakeWs([{"q": [.1] * 14, "q_age_s": .08}])))
    assert sink.onboard_q_t == pytest.approx(99.92)


def test_feedback_outbox_bounds_memory_and_carries_queue_age(monkeypatch):
    async def run():
        cmd = armed_commander()
        clock = [10.0]
        monkeypatch.setattr(ob.time, "monotonic", lambda: clock[0])
        out = ob.FeedbackOutbox(capacity=2)
        await out.put({"q": [0.] * 14, "q_age_s": .01})
        await out.put({"ack": 2})
        blocked = asyncio.create_task(out.put({"ack": 3}))
        await asyncio.sleep(0)
        assert not blocked.done()
        clock[0] += .05
        sent = []
        class Socket:
            async def send_json(self, item):
                sent.append(item)
        await out.send_next(Socket(), cmd)
        await blocked
        assert sent[0]["q_age_s"] == pytest.approx(.06)
        assert sent[0]["feedback_queue_age_s"] == pytest.approx(.05)
    asyncio.run(run())


def test_feedback_write_has_a_deadline():
    async def run():
        out = ob.FeedbackOutbox(write_timeout=.01)
        await out.put({"ack": 1})
        class Socket:
            async def send_json(self, item):
                await asyncio.Event().wait()
        with pytest.raises(TimeoutError):
            await out.send_next(Socket(), armed_commander())
    asyncio.run(run())


@pytest.mark.parametrize('reason', ['stale', 'transition'])
def test_queued_target_cannot_refresh_watchdog_after_stall_or_rearm(reason):
    cmd = armed_commander()
    target = ob._prepare_target_frame(cmd, {'seq': 1, 'arm': [.1] * 14})
    if reason == 'stale':
        target['admission'] = (0, ob.time.monotonic() - 1)
    else:
        cmd._target_epoch = 1
    before = cmd.last_frame_t
    ack = ob._apply_target_frame(cmd, target)
    assert not ack['applied']
    assert ack['refused'] in ('stale_target', 'control_transition')
    assert cmd.last_frame_t == before
    assert not cmd.arm.commands


def test_mailbox_receipt_history_is_bounded():
    box = ob.LatestTargetMailbox(max_superseded=2)
    for seq in range(3):
        box.put({'seq': seq})
    with pytest.raises(OverflowError):
        box.put({'seq': 3})


@pytest.mark.parametrize('hz', [100, 250])
def test_real_publisher_keeps_motion_bounds_at_evaluated_cadences(fake_sdk, monkeypatch, hz):
    import numpy as np
    from tests.test_motor_smoothing import publisher_trace
    _, q = publisher_trace(monkeypatch, [0.] * hz + [1.] * (hz * 2) + [-1.] * (hz * 2), dt=1/hz)
    velocity = np.diff(q, axis=0) * hz
    acceleration = np.diff(velocity, axis=0) * hz
    assert np.max(np.abs(velocity)) <= 35 + 1e-6
    assert np.max(np.abs(acceleration)) <= 16 + 1e-6


def test_expired_feedback_closes_instead_of_replaying_old_state(monkeypatch):
    async def run():
        clock = [10.]
        # Patch only onboard's module clock; leave asyncio's scheduler real.
        monkeypatch.setattr(ob, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        out = ob.FeedbackOutbox()
        await out.put({'state': 'ARMED'})
        clock[0] += .2
        with pytest.raises(TimeoutError):
            await out.send_next(None, None)
    asyncio.run(run())

