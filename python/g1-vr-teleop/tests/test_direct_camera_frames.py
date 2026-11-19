"""The headset video path uses the camera service, never detector previews."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "teleop" / "bridge"))
from cam_client import CameraServiceClient, camera_frame_url  # noqa: E402


JPEG = b"\xff\xd8\xff" + b"direct-camera" * 20 + b"\xff\xd9"


def _client():
    client = CameraServiceClient.__new__(CameraServiceClient)
    client._init_state("http://camera:8001/frame.jpg", None)
    client._seq = 0
    client._last_identity = None
    client.width = 640
    client.height = 480
    client.stream_id = "stream-1"
    client.depth = None
    client._depth_capture = None
    return client


def test_camera_base_url_becomes_direct_jpeg_endpoint():
    assert camera_frame_url("http://camera:8001") == "http://camera:8001/frame.jpg"
    assert camera_frame_url("http://camera:8001/") == "http://camera:8001/frame.jpg"


def test_direct_frame_preserves_camera_identity_and_timestamp():
    received = []
    client = _client()
    client.on_frame = lambda header, jpeg: received.append((header, jpeg))
    assert client._handle_jpeg(JPEG, frame_id=42, captured_at_unix_ns=1789486133350944706)
    header, jpeg = received[0]
    assert jpeg == JPEG
    assert header["src"] == "camera" and header["frame_id"] == 42
    assert header["w"] == 640 and header["h"] == 480
    assert header["t"] == pytest.approx(1789486133.3509, abs=0.001)


def test_duplicate_camera_snapshot_is_not_counted_twice():
    client = _client()
    assert client._handle_jpeg(JPEG, frame_id=42, captured_at_unix_ns=100)
    assert not client._handle_jpeg(JPEG, frame_id=42, captured_at_unix_ns=100)
    assert client.n_frames == 1


def test_non_jpeg_direct_frame_is_rejected():
    client = _client()
    assert not client._handle_jpeg(b"not-jpeg", frame_id=1, captured_at_unix_ns=100)
    assert client.n_bad == 1 and client.n_frames == 0
