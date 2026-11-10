"""The optional native recorder must not cost the default teleop path."""

from types import SimpleNamespace


def test_onboard_lowstate_capture_precedes_status_decimation():
    from g1_onboard import Commander

    commander = Commander()
    try:
        commander.telemetry.subscribers = 1
        commander.telemetry.start_worker()
        commander.on_lowstate({"raw": [0.123456789012345]})
        import time
        deadline = time.time() + 1
        while not commander.telemetry.since(0)["readings"] and time.time() < deadline:
            time.sleep(.005)
        batch = commander.telemetry.since(0)
        assert batch["readings"][0]["stream"] == "rt/lowstate"
        assert batch["readings"][0]["payload"] == {
            "raw": [0.123456789012345]
        }
    finally:
        commander.telemetry.close()
        commander._stop = True
