"""Left squeeze records; home belongs to both stick clicks."""
import asyncio
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))
import server                                                    # noqa: E402


def _frame(left=0.0, right=0.0, source="controller"):
    return {"hands": [{"hand": "left", "source": source, "squeeze": left},
                      {"hand": "right", "source": source, "squeeze": right}]}


class _Rec:
    def __init__(self, running=False):
        self.recording = running          # the PUBLIC property, as the code reads
        self.calls = []
    def start(self, name="", meta=None):
        self.calls.append(("start", name)); self.recording = True
        return {"ok": True, "session": "S"}
    def stop(self):
        self.calls.append(("stop",)); self.recording = False
        return {"ok": True, "session": "S"}
    def path(self, sid): return None       # nothing to check or seal in a test


def _app(rec=None, sink=None):
    return {"session": rec, "sink": sink, "arm_mode": "geometry",
            "camera": None, "detector": None, "retargeter": None,
            "_onboard_status": {"data": {}}, "source": "xr"}


def _drain(coro):
    """The squeeze RETURNS the work; run it here for the assertion.

    It returns a coroutine rather than scheduling one so it assumes nothing
    about a running loop -- which is exactly what makes this callable.
    """
    if coro is not None:
        asyncio.run(coro)


class _Sink:
    def __init__(self): self.cmds = []
    def command(self, name): self.cmds.append(name); return True


# ---- the edge ---------------------------------------------------------------

def test_one_pull_is_one_action_however_long_it_is_held():
    st, rec = {}, _Rec()
    for _ in range(20):
        _drain(server.record_on_squeeze(_frame(left=1.0), st, _app(rec)))
    assert rec.calls == [("start", "squeeze")]


def test_it_fires_again_after_a_release():
    st, rec = {}, _Rec()
    _drain(server.record_on_squeeze(_frame(left=1.0), st, _app(rec)))
    _drain(server.record_on_squeeze(_frame(left=0.0), st, _app(rec)))
    _drain(server.record_on_squeeze(_frame(left=1.0), st, _app(rec)))
    assert [c[0] for c in rec.calls] == ["start", "stop"]


def test_a_finger_trembling_at_the_threshold_does_not_toggle_repeatedly():
    """Hysteresis. Without it a squeeze resting on the line starts and stops
    the recorder several times a second."""
    st, rec = {}, _Rec()
    for v in (0.7, 0.55, 0.62, 0.5, 0.58, 0.65):
        _drain(server.record_on_squeeze(_frame(left=v), st, _app(rec)))
    assert len(rec.calls) == 1, rec.calls


def test_a_light_touch_is_not_a_press():
    st, rec = {}, _Rec()
    for v in (0.1, 0.3, 0.5):
        _drain(server.record_on_squeeze(_frame(left=v), st, _app(rec)))
    assert rec.calls == []


# ---- record -----------------------------------------------------------------

def test_left_squeeze_starts_a_stopped_recorder_and_stops_a_running_one():
    st = {}
    stopped, running = _Rec(False), _Rec(True)
    _drain(server.record_on_squeeze(_frame(left=1.0), st, _app(stopped)))
    _drain(server.record_on_squeeze(_frame(left=1.0), {}, _app(running)))
    assert stopped.calls[0][0] == "start"
    assert running.calls[0][0] == "stop"


def test_which_way_it_went_is_asked_not_assumed():
    """The recorder is the truth; a flag here would drift from it the first
    time a session was stopped from the page instead."""
    st, rec = {}, _Rec(False)
    _drain(server.record_on_squeeze(_frame(left=1.0), st, _app(rec)))
    rec.recording = False                 # stopped elsewhere, e.g. the page
    _drain(server.record_on_squeeze(_frame(left=0.0), st, _app(rec)))
    _drain(server.record_on_squeeze(_frame(left=1.0), st, _app(rec)))
    assert [c[0] for c in rec.calls] == ["start", "start"]


def test_the_right_squeeze_does_not_touch_the_recorder():
    st, rec = {}, _Rec()
    _drain(server.record_on_squeeze(_frame(right=1.0), st, _app(rec)))
    assert rec.calls == []


# ---- the removed home binding -----------------------------------------------

@pytest.mark.parametrize("left,right", [(0, 1), (1, 0), (1, 1)])
def test_squeezes_do_not_send_motion_commands(left, right):
    sink = _Sink()
    sink.onboard_state = "ARMED"
    frame = _frame(left=left, right=right)
    assert server.arm_on_sticks(frame, {}, _app(sink=sink)) is None
    assert sink.cmds == []
    # The receive loop must not retain the old independent home binding.
    assert "home_on_squeeze(" not in (ROOT / "teleop" / "bridge" / "server.py").read_text()


def test_hand_tracked_frames_never_fire_either():
    st, rec, sink = {}, _Rec(), _Sink()
    f = _frame(left=1.0, right=1.0, source="hand")
    _drain(server.record_on_squeeze(f, st, _app(rec)))
    assert server.arm_on_sticks(f, st, _app(sink=sink)) is None
    assert rec.calls == [] and sink.cmds == []


def test_no_recorder_or_no_sink_is_not_an_error():
    assert server.record_on_squeeze(_frame(left=1.0), {}, _app(None)) is None
    assert server.arm_on_sticks(_frame(right=1.0), {}, _app()) is None


# ---- the review's finding ----------------------------------------------------

def test_a_squeeze_started_session_carries_the_header_meta():
    """The bug the review caught.

    record_on_squeeze called recorder.start() directly, so a session begun
    from the controller had no arm_mode, no build, no camera status and no
    run config -- and nothing downstream could tell, because a
    header without config is also what every episode before that field existed
    looks like. It goes through start_session now.
    """
    seen = {}

    class _Capture(_Rec):
        def start(self, name="", meta=None):
            seen["meta"] = meta
            return super().start(name)

    rec = _Capture()
    _drain(server.record_on_squeeze(_frame(left=1.0), {}, _app(rec)))
    meta = seen.get("meta") or {}
    for key in ("arm_mode", "build", "camera", "config"):
        assert key in meta, f"a squeeze-started session lost {key}"


def test_a_squeeze_stopped_session_is_checked_and_sealed():
    """Same for the other end: the training pipeline consumes only sealed
    manifests, and stop_session is where the seal happens."""
    import inspect
    src = inspect.getsource(server.record_on_squeeze)
    assert "stop_session" in src and "start_session" in src
    assert "recorder.stop()" not in src and "recorder.start(" not in src


def test_stopping_is_scheduled_rather_than_awaited_in_the_frame_loop():
    """Stopping hashes the whole file and every camera frame. Awaiting that in
    the frame loop stalls it for seconds, and the onboard's liveness watchdog
    freezes the arms after one."""
    import inspect
    import re
    src = inspect.getsource(server.record_on_squeeze)
    # The BODY, not the docstring -- which says the word "await" while
    # explaining why it does not use one.
    body = re.sub(r'""".*?"""', "", src, flags=re.S)
    assert "await " not in body, "the frame loop must not wait for a seal"
    assert "ensure_future" not in body and "create_task" not in body, \
        "scheduling belongs to the caller; this must stay loop-agnostic"
    handler = inspect.getsource(server.ws_handler)
    assert "ensure_future(_rec_task)" in handler, "the caller must schedule it"
