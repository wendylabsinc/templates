"""Check the go2-sim template's variables, manifests, Dockerfile and rendering.

`wendy init` renders every recognized text file as a Go text/template. The
simulator ships large vendored JavaScript, so one stray "{{" in it would make
init fail or change the file. These tests render the template the way the CLI
does and prove that every file except the three templated ones comes out
byte-for-byte unchanged, including the vendored Three.js modules.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "python/go2-sim"
TEMPLATED = {"Dockerfile", "wendy.json", "README.md"}
ESCAPED = "go2_sim/vendor/three.core.js"

# Upstream SHA-256 values recorded in UPSTREAM.md. OrbitControls.js has one
# documented local import change, so its vendored hash differs from upstream.
VENDORED_SHA256 = {
    "go2_sim/vendor/three.core.js": "eb077d2417f61d3e6d9264c317cabc4ea35769ed6b0ab533067292a550784c20",
    "go2_sim/vendor/three.module.js": "c8211c69345d2e9949dc7a8ac969380497aa0600a5a8ac6a459c8cd02dd9cb8a",
    "go2_sim/vendor/OrbitControls.js": "06864a0fcb647730bfbc690b6c25a121199d716a3e299a40158509cbd247c3bf",
}

# Mirrors isTextFile in the Wendy CLI (go/internal/cli/commands/template.go).
TEXT_EXTENSIONS = {
    ".json", ".toml", ".yaml", ".yml", ".md", ".txt", ".html", ".css",
    ".js", ".ts", ".py", ".rs", ".swift", ".go", ".mojo",
    ".cpp", ".c", ".h", ".hpp", ".cmake", ".sh", ".bash", ".zsh",
    ".dockerfile", ".gitignore", ".env", ".cfg", ".ini", ".xml",
    ".svg", ".lock",
}
TEXT_NAMES = {
    "Dockerfile", "Makefile", "CMakeLists.txt", "Package.swift",
    "Cargo.toml", "Cargo.lock", ".swift-version", ".gitignore",
}
ACTION = re.compile(rb"\{\{(.*?)\}\}", re.S)


def is_text_file(relative: str) -> bool:
    path = Path(relative)
    return path.suffix.lower() in TEXT_EXTENSIONS or path.name in TEXT_NAMES


def template_files() -> dict[str, bytes]:
    """Files wendy init writes: everything in the template except its manifests."""
    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--cached", "--others",
         "--exclude-standard", "--", str(TEMPLATE.relative_to(REPO_ROOT))],
        capture_output=True, check=True, text=True,
    ).stdout
    files = {}
    for name in filter(None, listing.split("\0")):
        path = REPO_ROOT / name
        relative = path.relative_to(TEMPLATE).as_posix()
        if path.is_file() and relative not in {"template.json", "template.schema.json"}:
            files[relative] = path.read_bytes()
    return files


def render(content: bytes, values: dict) -> bytes:
    """Render the Go template subset this template uses; reject anything else."""
    out, position = [], 0
    for match in ACTION.finditer(content):
        literal = content[position:match.start()]
        assert b"{{" not in literal
        out.append(literal)
        action = match.group(1).decode()
        if action == '"{{"':
            out.append(b"{{")
        elif action.startswith(".") and action[1:] in values:
            out.append(str(values[action[1:]]).encode())
        else:
            raise AssertionError(f"unsupported template action: {action!r}")
        position = match.end()
    tail = content[position:]
    assert b"{{" not in tail
    out.append(tail)
    return b"".join(out)


def render_project(values: dict) -> dict[str, bytes]:
    return {relative: render(content, values) if is_text_file(relative) else content
            for relative, content in template_files().items()}


def manifest_variables() -> dict:
    manifest = json.loads((TEMPLATE / "template.json").read_text())
    assert manifest["name"] == "go2-sim"
    return {variable["name"]: variable for variable in manifest["variables"]}


def test_template_variables():
    variables = manifest_variables()
    assert set(variables) == {"APP_ID", "PORT"}
    assert variables["APP_ID"]["type"] == "string"
    assert variables["APP_ID"]["required"] is True
    port = variables["PORT"]
    assert port["type"] == "integer"
    assert port["default"] == 8890
    assert port["validate"] == {"min": 1, "max": 65535}
    assert "viewer" in port["prompt"].lower()


def test_wendy_json_uses_variables_and_plain_host_networking():
    raw = (TEMPLATE / "wendy.json").read_text()
    assert '"appId": "{{.APP_ID}}"' in raw
    assert '"port": {{.PORT}}' in raw
    assert "host-admin" not in raw
    config = json.loads(render(raw.encode(), {"APP_ID": "my-sim", "PORT": 8890}))
    assert config["appId"] == "my-sim"
    assert config["entitlements"] == [{"type": "network", "mode": "host"}]
    assert config["readiness"]["tcpSocket"] == {"port": 8890}
    assert config["frameworks"]["ros2"] == {
        "distro": "humble", "rmw": "rmw_cyclonedds_cpp", "domainId": 0, "scope": "device",
    }


def test_dockerfile_port_visuals_and_cmake_probe():
    dockerfile = (TEMPLATE / "Dockerfile").read_text()
    final_stage = dockerfile.rsplit("\nFROM ", 1)[1]
    env_lines = [line for line in final_stage.splitlines() if line.startswith("ENV ")]
    assert any("GO2_PORT={{.PORT}}" in line for line in env_lines), "the final stage must set GO2_PORT"
    assert "\nEXPOSE {{.PORT}}\n" in final_stage
    assert "EXPOSE 8890" not in dockerfile

    # Lock regenerated on whatever architecture builds the image, then verified.
    assert ("RUN python3 tools/build_visuals.py --update-lock && "
            "python3 tools/build_visuals.py --check") in dockerfile
    assert "RUN python3 tools/build_visuals.py &&" not in dockerfile

    # x86 qemu-user workaround for the colcon step (WendyOS d34d29207).
    assert ("find_library(_wendy_ros_lib_probe NAMES builtin_interfaces__rosidl_generator_c "
            "PATHS /opt/ros/humble/lib NO_DEFAULT_PATH)") in dockerfile
    assert "> /tmp/wendy-ros-lib-probe.cmake" in dockerfile
    colcon = next(line for line in dockerfile.splitlines() if "colcon build" in line)
    assert "-DCMAKE_PROJECT_INCLUDE=/tmp/wendy-ros-lib-probe.cmake" in colcon


def test_only_the_expected_files_contain_template_delimiters():
    with_actions = {relative for relative, content in template_files().items()
                    if is_text_file(relative) and b"{{" in content}
    assert with_actions == TEMPLATED | {ESCAPED}
    escaped = (TEMPLATE / ESCAPED).read_bytes()
    assert {match.group(1) for match in ACTION.finditer(escaped)} == {b'"{{"'}


def test_no_path_is_renamed_by_init():
    # wendy init renames any path component equal to the template name.
    assert not [relative for relative in template_files() if "go2-sim" in relative.split("/")]


@pytest.mark.parametrize("port", [8890, 8891])
def test_rendering_keeps_every_other_file_byte_for_byte(port):
    values = {"APP_ID": "my-sim", "PORT": port}
    sources = template_files()
    rendered = render_project(values)
    assert set(rendered) == set(sources)

    for relative, content in rendered.items():
        if relative in TEMPLATED or relative == ESCAPED:
            continue
        assert content == sources[relative], f"rendering changed {relative}"

    dockerfile = rendered["Dockerfile"].decode()
    assert f"GO2_PORT={port}" in dockerfile and f"\nEXPOSE {port}\n" in dockerfile
    assert json.loads(rendered["wendy.json"])["readiness"]["tcpSocket"]["port"] == port
    readme = rendered["README.md"].decode()
    assert readme.startswith("# my-sim\n") and f":{port}" in readme


def test_vendored_three_js_survives_rendering_unchanged():
    upstream = (TEMPLATE / "UPSTREAM.md").read_text()
    rendered = render_project({"APP_ID": "my-sim", "PORT": 8890})
    for relative, expected in VENDORED_SHA256.items():
        assert expected in upstream, f"UPSTREAM.md no longer records {expected}"
        assert hashlib.sha256(rendered[relative]).hexdigest() == expected, relative
