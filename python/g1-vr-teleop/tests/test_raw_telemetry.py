import asyncio
import json
import os
from pathlib import Path
import time
from dataclasses import dataclass

from raw_telemetry import RawTelemetry


@dataclass
class Motor:
    q: float
    temperature: list


def test_raw_dds_readings_are_copied_without_rounding_or_filtering():
    log = RawTelemetry(capacity=2)
    log.subscribers = 1
    value = Motor(0.123456789012345, [25, 29])
    log.capture("rt/lowstate", value)
    value.q = 9
    value.temperature[0] = 99
    batch = log.since(0)
    reading = batch["readings"][0]
    assert reading["payload"] == {"q": 0.123456789012345, "temperature": [25, 29]}
    assert reading["source_at_unix_ns"] > 0


def test_slow_reader_gets_explicit_gap_and_never_stalls_publisher():
    log = RawTelemetry(capacity=2)
    log.subscribers = 1
    for i in range(4):
        log.capture("cmd", {"q": i})
    batch = log.since(0)
    assert batch["missing"] == 2
    assert [r["payload"]["q"] for r in batch["readings"]] == [2, 3]
    assert log.since(batch["last_sequence"])["readings"] == []


def test_no_recording_subscriber_means_no_telemetry_work():
    log = RawTelemetry()
    log.capture("cmd", object())
    assert log.since(0)["readings"] == []
    assert log.errors == 0


def test_deferred_capture_preserves_source_values_and_reports_bounded_drops():
    log = RawTelemetry(capacity=2)
    log.subscribers = 1
    log.start_worker()
    try:
        log.capture_async("rt/lowstate", Motor(0.123456789012345, [25]))
        deadline = time.time() + 1
        while not log.since(0)["readings"] and time.time() < deadline:
            time.sleep(.005)
        row = log.since(0)["readings"][0]
        assert row["stream"] == "rt/lowstate"
        assert row["payload"] == {"q": 0.123456789012345, "temperature": [25]}
        assert "deferred_drops" in log.since(0)
    finally:
        log.close()


def test_lowcmd_expands_the_immutable_crc_values_off_the_motor_thread():
    log = RawTelemetry()
    log.subscribers = 1
    log.start_worker()
    values = [2, 7]
    for i in range(35):
        values.extend([i, i + .1, i + .2, i + .3, i + .4, i + .5, 100 + i])
    values.extend([11, 22, 33, 44, 0xDEADBEEF])
    try:
        log.capture_lowcmd(tuple(values), 9)
        deadline = time.time() + 1
        while not log.since(0)["readings"] and time.time() < deadline:
            time.sleep(.005)
        payload = log.since(0)["readings"][0]["payload"]
        assert payload["write_result"] == 9
        assert payload["message"]["mode_machine"] == 7
        assert payload["message"]["motor_cmd"][34]["reserve"] == 134
        assert payload["message"]["crc"] == 0xDEADBEEF
    finally:
        log.close()


def test_passive_websocket_is_authenticated_and_delivers_original_values():
    from types import SimpleNamespace
    from aiohttp import web, WSServerHandshakeError
    from aiohttp.test_utils import TestClient, TestServer
    from raw_telemetry import readings_ws_handler
    import pytest

    async def scenario():
        log = RawTelemetry()
        app = web.Application()
        # No control methods exist: this route must not claim or command motors.
        app["cmd"] = SimpleNamespace(telemetry=log)
        app["secret"] = "test-key"
        app.router.add_get("/readings", readings_ws_handler)
        async with TestClient(TestServer(app)) as client:
            with pytest.raises(WSServerHandshakeError) as denied:
                await client.ws_connect("/readings?key=wrong")
            assert denied.value.status == 401 and log.subscribers == 0
            async with client.ws_connect("/readings?key=test-key") as ws:
                await ws.receive_json(timeout=1)
                log.capture("rt/lowstate", Motor(25.123456789012345, [25]))
                batch = await ws.receive_json(timeout=1)
                assert batch["readings"][0]["payload"]["q"] == 25.123456789012345
                assert batch["missing"] == 0
            for _ in range(100):
                if log.subscribers == 0:
                    break
                await asyncio.sleep(.01)
            assert log.subscribers == 0
    asyncio.run(scenario())


def _slow_test_encoder(batch):
    """Picklable test worker: emulate a stalled encoder without robot hardware."""
    if batch["readings"]:
        Path(batch["readings"][0]["payload"]["marker"]).write_text(str(os.getpid()))
        time.sleep(.4)
    return json.dumps(batch)


def test_slow_encoder_does_not_block_command_server(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    import raw_telemetry
    monkeypatch.setattr(raw_telemetry, "encode_readings_batch", _slow_test_encoder, raising=False)
    async def scenario():
        log = RawTelemetry()
        app = web.Application()
        app["cmd"] = SimpleNamespace(telemetry=log)
        app["secret"] = ""
        app.router.add_get("/readings", raw_telemetry.readings_ws_handler)
        async def control_probe(request):
            return web.json_response({"responsive": True})
        app.router.add_get("/control-probe", control_probe)
        marker = tmp_path / "encoder-pid"
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/readings", compress=15) as ws:
                await ws.receive_json(timeout=5)
                log.capture("cmd", {"marker": str(marker), "q": 25.123456789012345})
                for _ in range(200):
                    if marker.exists():
                        break
                    await asyncio.sleep(.01)
                assert marker.exists(), "encoder never left the control-server process"
                assert int(marker.read_text()) != os.getpid()
                start = time.monotonic()
                response = await client.get("/control-probe")
                assert (await response.json())["responsive"]
                assert time.monotonic() - start < .2
                batch = await ws.receive_json(timeout=2)
                assert batch["readings"][0]["payload"]["q"] == 25.123456789012345
    asyncio.run(scenario())

