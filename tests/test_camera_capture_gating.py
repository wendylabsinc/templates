"""Regression tests for camera-feed-yolo capture-device gating.

The stream path used to fall back to /dev/video0 unconditionally. On a board
whose only /dev/video* nodes are mem2mem codecs (e.g. qcom-iris on the
Dragonwing IQ-8275) that node can never produce frames, so the capture loop
retried it every 2 seconds forever, emitting ~8 lines of V4L2/FFMPEG errors per
attempt while the UI sat on a spinner with no explanation.

These tests import the template's app.py with its runtime dependencies stubbed,
then simulate a device that exposes only non-capture nodes.
"""
from __future__ import annotations

import pathlib
import sys
import types
from unittest import mock

import pytest

APP_PY = pathlib.Path(__file__).resolve().parents[1] / "python" / "camera-feed-yolo" / "app.py"

# A device whose only /dev/video* nodes are encode/decode, not capture.
CODEC_ONLY_NODES = ["/dev/video0", "/dev/video1"]


def _stub(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _Accepts:
    """Stand-in for classes the module instantiates at import time."""

    def __init__(self, *args, **kwargs):
        pass


class _YOLOStub(_Accepts):
    """YOLO stand-in; `names` keeps the background model loader quiet."""

    names = {0: "person"}

    def __call__(self, *args, **kwargs):
        return []


class _FastAPIStub(_Accepts):
    def _decorator(self, *args, **kwargs):
        return lambda fn: fn

    get = post = websocket = on_event = _decorator

    def mount(self, *args, **kwargs):
        pass


@pytest.fixture(scope="module")
def app_module():
    """Import the rendered template module with heavy deps replaced by stubs."""
    cv2 = _stub(
        "cv2",
        VideoCapture=mock.MagicMock(),
        imencode=mock.MagicMock(),
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        IMWRITE_JPEG_QUALITY=1,
    )
    _stub("numpy", ndarray=object, frombuffer=mock.MagicMock())
    _stub("ultralytics", YOLO=_YOLOStub)
    _stub("fastapi", FastAPI=_FastAPIStub, WebSocket=object, WebSocketDisconnect=Exception)
    _stub("fastapi.responses", FileResponse=_Accepts, JSONResponse=_Accepts)
    _stub("fastapi.staticfiles", StaticFiles=_Accepts)

    source = APP_PY.read_text().replace("{{.PORT}}", "3005").replace("{{.APP_ID}}", "test-app")
    module = types.ModuleType("yolo_app_under_test")
    module.__dict__["__file__"] = str(APP_PY)
    exec(compile(source, str(APP_PY), "exec"), module.__dict__)  # noqa: S102
    module.cv2 = cv2
    return module


def test_first_capture_device_rejects_codec_only_nodes(app_module):
    """A board with no real camera must yield no capture device."""
    with mock.patch.object(app_module, "_linux_candidate_video_nodes", return_value=CODEC_ONLY_NODES), \
         mock.patch.object(app_module, "_v4l2_is_capture", return_value=False), \
         mock.patch.object(app_module, "IS_MACOS", False):
        assert app_module._first_capture_device() is None
        assert app_module._enumerate_linux_cameras() == []


def test_first_capture_device_picks_the_capture_node(app_module):
    """When one node is a real capture source, it is the one selected."""
    with mock.patch.object(app_module, "_linux_candidate_video_nodes", return_value=CODEC_ONLY_NODES), \
         mock.patch.object(app_module, "_v4l2_is_capture", side_effect=lambda p: p == "/dev/video1"), \
         mock.patch.object(app_module, "IS_MACOS", False):
        assert app_module._first_capture_device() == "/dev/video1"


def test_capture_loop_backs_off_and_never_opens_a_non_capture_node(app_module):
    """The loop must gate on _v4l2_is_capture and back off exponentially.

    Before the fix this called cv2.VideoCapture three times per pass at a flat
    2 s interval, forever.
    """
    camera = app_module.YOLOCamera()
    app_module.cv2.VideoCapture.reset_mock()
    sleeps: list[float] = []

    def _record_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 8:
            raise SystemExit  # break out of the intentionally infinite loop

    with mock.patch.object(app_module, "_v4l2_is_capture", return_value=False), \
         mock.patch.object(app_module.time, "sleep", side_effect=_record_sleep):
        with pytest.raises(SystemExit):
            camera._opencv_loop("/dev/video0")

    assert app_module.cv2.VideoCapture.call_count == 0, "must not open a non-capture node"
    assert sleeps == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0, 30.0]
    assert max(sleeps) == app_module._CAPTURE_RETRY_MAX_S


def test_status_broadcast_is_deduplicated(app_module):
    """A retry loop should notify once per state change, not once per attempt."""
    camera = app_module.YOLOCamera()
    camera._loop = mock.MagicMock()
    camera.queues = {object(): mock.MagicMock(full=lambda: False)}

    camera._broadcast_status("no_capture_device", "No camera detected.")
    first = camera._loop.call_soon_threadsafe.call_count
    camera._broadcast_status("no_capture_device", "No camera detected.")
    second = camera._loop.call_soon_threadsafe.call_count

    assert first == 1
    assert second == 1, "identical consecutive status must not be re-sent"
