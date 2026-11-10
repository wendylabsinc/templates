"""The Quest control stream must stay newer than cosmetic rendering.

The failure this locks down was a live WebSocket that stopped carrying poses:
the bridge showed one connected Quest client forever while the last controller
frame aged by minutes.  Rendering ran before the only ws.send(), and the page
had neither backpressure nor a heartbeat to distinguish a sleeping XR session
from a render exception or a network queue.
"""

from pathlib import Path
import json
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "teleop" / "web" / "teleop.js").read_text()
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))
import server  # noqa: E402
NODE = shutil.which("node")


def _frame_loop():
    return JS.split("const onFrame = (t, frame) => {")[1].split(
        "session.requestAnimationFrame(onFrame);\n}", 1)[0]


def test_pose_send_precedes_all_cosmetic_rendering():
    """HUD or ghost failures must not prevent this frame's pose send."""
    body = _frame_loop()
    send = body.index("sendPoseFrame(")
    for draw in ("drawOverlayView(", "ghost.drawGhost(",
                 "ghost.ghostToggle(", "fb.hapticPlan("):
        assert send < body.index(draw), f"{draw} still runs before the control send"


def test_cosmetic_rendering_is_rate_limited_without_rate_limiting_pose_capture():
    body = _frame_loop()
    assert "RENDER_INTERVAL_MS" in JS
    assert body.index("sendPoseFrame(") < body.index("lastRenderT")
    assert "if (t - lastRenderT < RENDER_INTERVAL_MS) return" in body


def test_socket_backpressure_is_latest_wins_not_a_pose_backlog():
    """A slow Wi-Fi interval drops superseded poses instead of queuing lag."""
    assert "ws.bufferedAmount" in JS
    assert "droppedBackpressure" in JS
    assert "function sendPoseFrame" in JS


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_send_pose_frame_sends_now_or_drops_without_queueing():
    marker = "function sendPoseFrame("
    start = JS.index(marker)
    brace = JS.index("{", start)
    depth, end = 0, brace
    while end < len(JS):
        if JS[end] == "{":
            depth += 1
        elif JS[end] == "}":
            depth -= 1
            if depth == 0:
                break
        end += 1
    fn = JS[start:end + 1]
    script = fn + r"""
      globalThis.WebSocket = {OPEN: 1};
      const sent = [];
      const stats = {maxBufferedBytes: 0, droppedBackpressure: 0,
                     seq: 0, poses: 0, sendErrors: 0};
      const clear = {readyState: 1, bufferedAmount: 0, send: x => sent.push(JSON.parse(x))};
      const queued = {readyState: 1, bufferedAmount: 12, send: x => sent.push(JSON.parse(x))};
      const first = sendPoseFrame(clear, {t: 1, hands: [1]}, stats);
      const second = sendPoseFrame(queued, {t: 2, hands: [2]}, stats);
      console.log(JSON.stringify({first, second, sent, stats}));
    """
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["first"] is True and out["second"] is False
    assert [m["t"] for m in out["sent"]] == [1]
    assert out["stats"]["droppedBackpressure"] == 1
    assert out["stats"]["maxBufferedBytes"] == 12


def test_xr_heartbeat_exposes_raf_tracking_buffer_and_render_failures():
    """A connected socket without poses must say *why* from the Quest side."""
    for field in ("raf_fps", "pose_fps", "camera_fps", "buffered_bytes", "render_errors",
                  "dropped_backpressure", "visibility", "xr_visibility"):
        assert field in JS, field
    assert 'type: "heartbeat"' in JS
    assert "clearInterval(streamHeartbeat)" in JS


def test_cosmetic_render_errors_are_contained_after_the_control_send():
    body = _frame_loop()
    assert "renderErrors++" in body
    assert body.index("sendPoseFrame(") < body.index("renderErrors++")


def test_bridge_distinguishes_sleep_tracking_loss_and_a_slow_pose_stream():
    base = {"connected": True, "clients": 1, "last_heartbeat_t": 99.8,
            "last_pose_t": 99.9, "client": {"tracked_inputs": 2}}
    assert server.xr_stream_status(base, now=100.0)["state"] == "healthy"

    asleep = dict(base, last_heartbeat_t=94.0)
    assert server.xr_stream_status(asleep, now=100.0)["state"] == "client_stalled"

    lost = dict(base, last_pose_t=96.0, client={"tracked_inputs": 0})
    assert server.xr_stream_status(lost, now=100.0)["state"] == "tracking_lost"

    slow = dict(base, last_pose_t=99.0)
    assert server.xr_stream_status(slow, now=100.0)["state"] == "pose_stalled"



def test_room_fixed_panels_are_drawn_on_every_xr_frame():
    """At a 30 Hz budget a room-fixed panel is stale two frames in three and
    judders against the room as the head moves -- the operator saw it shake.
    Head-locked keeps the cheaper budget; the pose send is first either way."""
    assert ': (HUD_LOCK ? 0 : 30);' in JS
    assert JS.index("const HUD_LOCK") < JS.index("const RENDER_HZ"), "declared before use"
    assert "RENDER_HZ > 0 ? 1000 / RENDER_HZ : 0" in JS
