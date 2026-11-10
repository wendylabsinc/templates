"""Checks for the g1-vr-teleop template that need no robot, headset or deps.

The template's own behaviour tests (retargeting, safety gates, recording) live
in python/g1-vr-teleop/tests and need Pinocchio; see that README.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROJECT = ROOT / "python" / "g1-vr-teleop"
VARIABLES = {v["name"]: v for v in json.loads((PROJECT / "template.json").read_text())["variables"]}
ACTION = re.compile(r"{{(.*?)}}", re.DOTALL)
NAME = re.compile(r"\.([A-Z][A-Z0-9_]*)")


def _files():
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others",
                          "--exclude-standard", "python/g1-vr-teleop"],
                         capture_output=True, check=True, text=True).stdout
    return [ROOT / p for p in out.split("\0") if p]


def _render(text: str, values: dict[str, str]) -> str:
    return ACTION.sub(lambda m: values[NAME.search(m.group(1)).group(1)], text)


def test_every_placeholder_is_declared_and_every_variable_is_used():
    used = set()
    for path in _files():
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for body in ACTION.findall(text):
            used.update(NAME.findall(body))
    assert used <= set(VARIABLES), sorted(used - set(VARIABLES))
    assert set(VARIABLES) - {"APP_ID"} <= used, sorted(set(VARIABLES) - used)


def test_placeholders_only_in_deploy_files():
    # The engine renders every text file; Python and web code must not contain
    # template actions, or a stray "{{" would break or be rewritten at init.
    allowed = {"wendy.json", "Dockerfile", "README.md"}
    for path in _files():
        if path.name in allowed or path.name == "template.json":
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        assert "{{" not in text, f"{path} contains a template action"


def test_defaults_satisfy_their_own_validation():
    for var in VARIABLES.values():
        if "default" not in var:
            continue
        rule = var.get("validate", {})
        if "pattern" in rule:
            assert re.fullmatch(rule["pattern"], str(var["default"])), var["name"]
        if "min" in rule:
            assert rule["min"] <= var["default"] <= rule["max"], var["name"]


def test_wendy_json_renders_to_valid_json():
    values = {name: str(v.get("default", "x" * 16)) for name, v in VARIABLES.items()}
    values["APP_ID"] = "g1-teleop"
    manifest = json.loads(_render((PROJECT / "wendy.json").read_text(), values))
    assert set(manifest["services"]) == {"onboard", "teleop"}
    assert manifest["readiness"]["tcpSocket"]["port"] == VARIABLES["HTTPS_PORT"]["default"]


def test_onboard_and_bridge_use_the_same_arm_limits():
    onboard = (PROJECT / "onboard" / "calibration" / "unitree-g1-nx-2.json").read_bytes()
    bridge = (PROJECT / "teleop" / "calibration" / "arm-limits.json").read_bytes()
    assert onboard == bridge


def test_onboard_entrypoint_maps_deadman_and_walking():
    script = (PROJECT / "onboard" / "entrypoint.sh").read_text()
    assert 'none) set -- "$@" --no-deadman' in script
    assert 'set -- "$@" --deadman "$G1_DEADMAN"' in script
    assert 'on) set -- "$@" --locomotion' in script
