"""Check the so101-sim manifest, port wiring, and that wendy init rendering leaves the simulator intact."""
import json
from pathlib import Path
import re

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "python/so101-sim"

# Mirrors isTextFile in WendyOS go/internal/cli/commands/template.go: wendy init
# renders these files as Go text/template and copies every other file unchanged.
RENDERED_EXTENSIONS = {
    ".json", ".toml", ".yaml", ".yml", ".md", ".txt", ".html", ".css",
    ".js", ".ts", ".py", ".rs", ".swift", ".go", ".mojo",
    ".cpp", ".c", ".h", ".hpp", ".cmake", ".sh", ".bash", ".zsh",
    ".dockerfile", ".gitignore", ".env", ".cfg", ".ini", ".xml",
    ".svg", ".lock",
}
RENDERED_NAMES = {
    "Dockerfile", "Makefile", "CMakeLists.txt", "Package.swift",
    "Cargo.toml", "Cargo.lock", ".swift-version", ".gitignore",
}
NOT_WRITTEN = {"template.json", "template.schema.json"}
# The only files that use template variables; everything else is the simulator as copied.
TEMPLATED = {"Dockerfile", "README.md", "wendy.json", "so101_sim/server.py"}
ACTION = re.compile(rb"{{(.*?)}}", re.DOTALL)


def template_files():
    return sorted(p for p in TEMPLATE.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def is_rendered(path):
    # Go's filepath.Ext treats a leading dot as an extension, so ".dockerignore" is one.
    name = path.name
    ext = name[name.rfind("."):].lower() if "." in name else ""
    return ext in RENDERED_EXTENSIONS or name in RENDERED_NAMES


def render(content, values):
    """Go text/template copies text outside {{ }} verbatim; resolve each action to a declared value."""
    def action(match):
        body = match.group(1).decode()
        name = re.fullmatch(r"\.([A-Z][A-Z0-9_]*)", body)
        assert name, f"unsupported template action {{{{{body}}}}}"
        assert name.group(1) in values, f"undeclared template variable {name.group(1)}"
        return str(values[name.group(1)]).encode()
    return ACTION.sub(action, content)


def manifest():
    return json.loads((TEMPLATE / "template.json").read_text())


def variable(name):
    return next(v for v in manifest()["variables"] if v["name"] == name)


def test_template_json_declares_app_id_and_port():
    assert manifest()["name"] == "so101-sim"
    assert [v["name"] for v in manifest()["variables"]] == ["APP_ID", "PORT"]
    app_id = variable("APP_ID")
    assert app_id["type"] == "string" and app_id["required"] is True
    port = variable("PORT")
    assert port["type"] == "integer"
    # 8890 is the Go2 simulator's port; the arm must be able to run next to it.
    assert port["default"] == 8891
    assert port["validate"] == {"min": 1, "max": 65535}
    assert "port" in port["prompt"].lower()


def test_wendy_json_uses_app_id_port_and_plain_host_networking():
    text = (TEMPLATE / "wendy.json").read_text()
    assert '"appId": "{{.APP_ID}}"' in text
    wendy = json.loads(render(text.encode(), {"APP_ID": "my-arm-sim", "PORT": 8891}))
    assert wendy["appId"] == "my-arm-sim"
    assert wendy["entitlements"] == [{"type": "network", "mode": "host"}]
    assert wendy["readiness"]["tcpSocket"] == {"port": 8891}
    assert "{{.PORT}}" in text and "8890" not in text


def test_dockerfile_and_startup_message_use_port():
    dockerfile = (TEMPLATE / "Dockerfile").read_text()
    assert "EXPOSE {{.PORT}}\n" in dockerfile
    assert '"--port", "{{.PORT}}"]' in dockerfile
    server = (TEMPLATE / "so101_sim/server.py").read_text()
    assert "HTTP :{{.PORT}}" in server
    for text in (dockerfile, server):
        assert "8890" not in text


def test_only_the_expected_files_use_template_actions():
    used = {}
    for path in template_files():
        content = path.read_bytes()
        if b"{{" in content:
            used[path.relative_to(TEMPLATE).as_posix()] = {a.decode() for a in ACTION.findall(content)}
    assert set(used) == TEMPLATED
    assert set().union(*used.values()) == {".APP_ID", ".PORT"}


@pytest.mark.parametrize("values", [
    {"APP_ID": "my-arm-sim", "PORT": 8891},
    {"APP_ID": "arm", "PORT": 9000},
])
def test_rendering_leaves_non_templated_files_byte_identical(values):
    declared = {v["name"] for v in manifest()["variables"]}
    assert set(values) == declared
    for path in template_files():
        relative = path.relative_to(TEMPLATE)
        if relative.as_posix() in NOT_WRITTEN:
            continue
        # wendy init renames any directory named after the template to the app ID.
        assert "so101-sim" not in relative.parts[:-1]
        source = path.read_bytes()
        output = render(source, values) if is_rendered(path) else source
        if relative.as_posix() in TEMPLATED:
            assert b"{{" not in output and str(values["PORT"]).encode() in output
        else:
            assert output == source, f"{relative} changes when rendered"


def test_simulator_assets_are_copied_unrendered_or_brace_free():
    # The viewer (HTML/JS), MJCF and lock files are rendered by wendy init because
    # of their extensions, so none of them may ever contain a Go template action.
    for name in ("so101_sim/index.html", "so101_sim/viewer.js", "so101_sim/lidar-view.js",
                 "model/scene.xml", "assets.lock.json", "viewer-assets.lock.json"):
        assert is_rendered(TEMPLATE / name)
        assert b"{{" not in (TEMPLATE / name).read_bytes()
    for name in (".dockerignore", "licenses/SO-ARM100.LICENSE", "licenses/three.LICENSE"):
        assert not is_rendered(TEMPLATE / name)
