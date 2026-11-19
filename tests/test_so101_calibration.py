"""Check the so101-calibration serial-port choice with fake ports, and its safety rules."""
import importlib.util
from pathlib import Path
import re

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "python/so101-calibration"
spec = importlib.util.spec_from_file_location("so101_port_detect", TEMPLATE / "port_detect.py")
port_detect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(port_detect)

IDS = [1, 2, 3, 4, 5, 6]
BY_ID = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5C4C127597-if00"


def fake_dev(paths, links=None):
    """Return glob and realpath functions for a fake /dev with the given nodes and symlinks."""
    links = links or {}
    entries = list(paths) + list(links)

    def glob_fn(pattern):
        prefix = pattern.rstrip("*")
        return [p for p in entries if p.startswith(prefix)]

    def realpath_fn(path):
        return links.get(path, path)

    return {"glob_fn": glob_fn, "realpath_fn": realpath_fn}


def ping_table(table):
    """ping_port fake: device -> list of answering IDs, or an exception to raise."""
    calls = []

    def ping_port(device):
        calls.append(device)
        answer = table.get(device, [])
        if isinstance(answer, Exception):
            raise answer
        return answer

    ping_port.calls = calls
    return ping_port


@pytest.mark.parametrize("value", [None, "", "  ", "auto", "AUTO"])
def test_unset_or_auto_means_detect(value):
    assert port_detect.configured_port(value) is None


def test_explicit_port_is_kept():
    assert port_detect.configured_port(" /dev/ttyACM1 ") == "/dev/ttyACM1"


def test_candidates_dedupe_by_id_links_and_sort_naturally():
    dev = fake_dev(
        ["/dev/ttyUSB0", "/dev/ttyACM10", "/dev/ttyACM2", "/dev/ttyACM0"],
        {BY_ID: "/dev/ttyACM0"},
    )
    candidates = port_detect.candidate_ports(**dev)
    assert [c.device for c in candidates] == ["/dev/ttyACM0", "/dev/ttyACM2", "/dev/ttyACM10", "/dev/ttyUSB0"]
    assert candidates[0].alias == BY_ID.rsplit("/", 1)[1]
    assert candidates[0].label() == f"/dev/ttyACM0 ({candidates[0].alias})"
    assert candidates[1].alias is None


def test_auto_picks_the_port_where_servos_answer():
    # The test rig: the arm's CH343 adapter is ttyACM0; another tty is silent.
    dev = fake_dev(["/dev/ttyACM0", "/dev/ttyACM1"], {BY_ID: "/dev/ttyACM0"})
    ping = ping_table({"/dev/ttyACM0": IDS, "/dev/ttyACM1": []})
    choice = port_detect.select_port(None, ping, IDS, **dev)
    assert choice.device == "/dev/ttyACM0"
    assert choice.auto
    assert "auto-detected /dev/ttyACM0 (usb-1a86_USB_Single_Serial_5C4C127597-if00)" in choice.message
    assert "6/6 servos answered" in choice.message
    assert ping.calls == ["/dev/ttyACM0", "/dev/ttyACM1"]


def test_auto_skips_a_silent_first_port():
    dev = fake_dev(["/dev/ttyACM0", "/dev/ttyACM1"])
    choice = port_detect.select_port(None, ping_table({"/dev/ttyACM1": IDS}), IDS, **dev)
    assert choice.device == "/dev/ttyACM1"


def test_auto_prefers_most_answers_and_names_the_other_arm():
    dev = fake_dev(["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0"])
    ping = ping_table({"/dev/ttyACM0": [1, 2], "/dev/ttyACM1": IDS, "/dev/ttyUSB0": IDS})
    choice = port_detect.select_port(None, ping, IDS, **dev)
    assert choice.device == "/dev/ttyACM1"  # tie with ttyUSB0 goes to the first port
    assert "servos also answer on /dev/ttyACM0, /dev/ttyUSB0" in choice.message
    assert "SO101_CALIB_PORT" in choice.message


def test_open_errors_do_not_stop_the_probe():
    dev = fake_dev(["/dev/ttyACM0", "/dev/ttyACM1"])
    ping = ping_table({"/dev/ttyACM0": PermissionError("permission denied"), "/dev/ttyACM1": IDS})
    choice = port_detect.select_port(None, ping, IDS, **dev)
    assert choice.device == "/dev/ttyACM1"


def test_partial_answers_name_the_missing_ids():
    dev = fake_dev(["/dev/ttyACM0"])
    choice = port_detect.select_port(None, ping_table({"/dev/ttyACM0": [1, 2, 3, 4]}), IDS, **dev)
    assert choice.device == "/dev/ttyACM0"
    assert "4/6 servos answered (no answer from id 5, 6" in choice.message


def test_no_visible_device_is_explained():
    choice = port_detect.select_port(None, ping_table({}), IDS, **fake_dev([]))
    assert choice.device is None
    assert "no serial device visible" in choice.message
    assert "wendy.json" in choice.message


def test_no_answer_is_explained_per_port():
    dev = fake_dev(["/dev/ttyACM0", "/dev/ttyUSB0"])
    ping = ping_table({"/dev/ttyACM0": [], "/dev/ttyUSB0": OSError("device busy")})
    choice = port_detect.select_port(None, ping, IDS, **dev)
    assert choice.device is None
    assert "no Feetech servos answered" in choice.message
    assert "/dev/ttyACM0: no servo answered" in choice.message
    assert "/dev/ttyUSB0: device busy" in choice.message
    assert "power supply" in choice.message


def test_configured_port_skips_the_scan():
    def no_glob(_pattern):
        raise AssertionError("an explicit port must not scan /dev")

    ping = ping_table({"/dev/ttyACM1": []})
    choice = port_detect.select_port("/dev/ttyACM1", ping, IDS, glob_fn=no_glob)
    assert choice.device == "/dev/ttyACM1"  # kept even when no servo answers
    assert not choice.auto
    assert "set by SO101_CALIB_PORT" in choice.message
    assert "0/6 servos answered" in choice.message
    assert ping.calls == ["/dev/ttyACM1"]


def test_configured_port_that_cannot_open_is_reported():
    ping = ping_table({"/dev/ttyACM1": FileNotFoundError("no such file")})
    choice = port_detect.select_port("/dev/ttyACM1", ping, IDS, glob_fn=lambda _p: [])
    assert choice.device is None
    assert "could not be opened: no such file" in choice.message


def test_app_never_writes_to_the_servos():
    # Safety: calibration keeps the arm limp, so the app must only ping and read.
    source = (TEMPLATE / "calibrate.py").read_text()
    writes = re.findall(r"\b(?:write\w*Tx\w*|regWrite\w*|syncWrite\w*|GroupSyncWrite)\b|\.action\(", source)
    assert not writes, f"calibrate.py writes to the servo bus: {writes}"


@pytest.mark.parametrize("name", ["calibrate.py", "port_detect.py"])
def test_python_sources_have_no_go_template_delimiters(name):
    # wendy init renders .py files as Go templates, so a stray "{{" breaks init.
    assert "{{" not in (TEMPLATE / name).read_text()
