"""TELEOP_RECORDING switches data collection on (default) or off.

Off means: no SessionRecorder exists, so nothing is written for a session;
/session/* and /record/* are not routes at all (404); /control/status says
`recording_enabled: false`, and the control page and the headset legend read
that to hide their recording controls. Teleop itself does not change.
"""
import asyncio
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest
from aiohttp.test_utils import TestClient, TestServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))

import server  # noqa: E402
from session_recorder import SessionRecorder  # noqa: E402


class _Sink(server.LogSink):
    def __init__(self):
        super().__init__()
        self.applied = 0
        self.onboard_state = "HOLDING"

    async def apply(self, targets):
        self.applied += 1


def _squeeze_frame(left):
    return {"hands": [{"hand": "left", "source": "controller", "squeeze": left,
                       "wrist": [0.2, -0.2, -0.05], "quat": [0.0, 0.0, 0.0, 1.0]}],
            "head": {"pos": [0.0, 1.6, 0.0], "quat": [0.0, 0.0, 0.0, 1.0]}}


# ---- the env switch ----------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "1", "on", "true", "yes", "anything"])
def test_recording_is_on_unless_explicitly_disabled(value):
    env = {} if value is None else {"TELEOP_RECORDING": value}
    assert server.recording_enabled(env) is True


@pytest.mark.parametrize("value", ["0", "off", "false", "no", "OFF", " No "])
def test_recording_can_be_switched_off(value):
    assert server.recording_enabled({"TELEOP_RECORDING": value}) is False


def test_make_app_reads_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEOP_SESSION_DIR", str(tmp_path))
    monkeypatch.setenv("TELEOP_RECORDING", "0")
    app = server.make_app(server.DebugRetargeter(), _Sink())
    assert app["session"] is None and app["recording_enabled"] is False
    monkeypatch.setenv("TELEOP_RECORDING", "1")
    app = server.make_app(server.DebugRetargeter(), _Sink())
    assert isinstance(app["session"], SessionRecorder)
    assert app["recording_enabled"] is True


# ---- routes and status, over HTTP ------------------------------------------------

async def _get(app, *paths):
    """GET each path, or POST it when written as "POST /path"."""
    out = {}
    async with TestClient(TestServer(app)) as client:
        for path in paths:
            method, _, url = path.rpartition(" ")
            resp = await (client.post if method == "POST" else client.get)(url)
            body = await resp.text()
            out[path] = (resp.status, body)
    return out


def test_on_registers_the_recording_routes_and_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEOP_SESSION_DIR", str(tmp_path))
    app = server.make_app(server.DebugRetargeter(), _Sink(), recording=True)
    app["camera"] = None
    got = asyncio.run(_get(app, "/session/status", "/session/list", "/control/status"))
    assert got["/session/status"][0] == 200
    assert got["/session/list"][0] == 200
    status = json.loads(got["/control/status"][1])
    assert status["recording_enabled"] is True
    assert status["session"] == {"recording": False, "id": None}


def test_off_leaves_the_recording_routes_unregistered(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEOP_SESSION_DIR", str(tmp_path))
    app = server.make_app(server.DebugRetargeter(), _Sink(), recording=False)
    app["camera"] = None
    got = asyncio.run(_get(app, "/session/status", "/session/list", "/record/dump",
                           "/control/status", "POST /session/start", "POST /record/start"))
    assert got["/session/status"][0] == 404
    assert got["/session/list"][0] == 404
    assert got["/record/dump"][0] == 404
    status = json.loads(got["/control/status"][1])
    assert status["recording_enabled"] is False
    assert status["session"] is None
    # A POST to a path no route claims is answered by the static file route,
    # which only takes GET/HEAD: 405 rather than 404, but nothing runs.
    assert got["POST /session/start"][0] in (404, 405)
    assert got["POST /record/start"][0] in (404, 405)


def test_off_still_serves_teleop_and_the_operator_pages(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEOP_SESSION_DIR", str(tmp_path))
    app = server.make_app(server.DebugRetargeter(), _Sink(), recording=False)
    app["camera"] = None
    got = asyncio.run(_get(app, "/index.html", "/control.html", "/waist", "/locomotion"))
    assert all(code == 200 for code, _ in got.values()), got


# ---- nothing is written while off ------------------------------------------------

async def _drive(app, sink, frames):
    async with TestClient(TestServer(app)) as client:
        async with client.ws_connect("/teleop") as ws:
            for f in frames:
                await ws.send_json(f)
                await asyncio.sleep(0.02)
            for _ in range(50):
                await asyncio.sleep(0.01)
                if sink.applied >= len(frames):
                    break


def test_off_writes_nothing_even_when_the_left_squeeze_is_pressed(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEOP_SESSION_DIR", str(tmp_path))
    sink = _Sink()
    app = server.make_app(server.DebugRetargeter(), sink, recording=False)
    app["camera"] = None
    frames = [_squeeze_frame(0.0), _squeeze_frame(1.0), _squeeze_frame(0.0)]
    asyncio.run(_drive(app, sink, frames))
    assert sink.applied >= 1, "teleop must still drive the sink with recording off"
    assert list(tmp_path.iterdir()) == [], "recording off must not write a session"


def test_on_the_left_squeeze_starts_a_session(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEOP_SESSION_DIR", str(tmp_path))
    sink = _Sink()
    app = server.make_app(server.DebugRetargeter(), sink, recording=True)
    app["camera"] = None
    asyncio.run(_drive(app, sink, [_squeeze_frame(0.0), _squeeze_frame(1.0)]))
    try:
        assert app["session"].recording, "the squeeze did not start a session"
        assert any(p.suffix == ".jsonl" for p in tmp_path.iterdir())
    finally:
        app["session"].close()


# ---- the UI hides what it cannot use --------------------------------------------

def test_the_control_page_hides_its_recording_controls_from_status():
    page = (ROOT / "teleop" / "web" / "control.html").read_text()
    assert 'id="recording-controls"' in page
    assert "s.recording_enabled !== false" in page
    assert '$("#recording-controls").hidden = !recordingOn' in page
    assert "[hidden] { display:none !important; }" in page, \
        "a flex row ignores the hidden attribute without this"


def test_the_headset_legend_drops_the_squeeze_row_when_recording_is_off():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    hud = (ROOT / "teleop" / "web" / "hud.js").as_uri()
    # Not an f-string: `wendy init` renders .py files as Go templates, so no
    # double opening brace here.
    script = """
    import('HUD_URI').then((h) => {
      console.log(JSON.stringify([h.hudFromStatus({recording_enabled: false}).recordingEnabled,
                                  h.hudFromStatus({recording_enabled: true}).recordingEnabled,
                                  h.hudFromStatus({}).recordingEnabled]));
    }).catch((e) => { console.error(e); process.exit(1); });
    """.replace("HUD_URI", hud)
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [False, True, True]
    src = (ROOT / "teleop" / "web" / "hud.js").read_text()
    rows = src.split("const rows = [")[1].split("];")[0]
    assert 'state.recordingEnabled === false ? [] :' in rows
    assert '"left squeeze"' in rows
