"""The hand-camera service: device choice, left/right, passthrough, swap.

The robot's two hand cameras are identical: same model, same USB serial. So
/dev/v4l/by-id collides and /dev/videoN moves between boots; the only stable
name is the USB port (/dev/v4l/by-path). These tests build a fake /dev and
/sys tree and check that the service picks the capture nodes by port, skips
metadata nodes and the RealSense, honours an explicit path, and remembers a
swap -- without a camera, OpenCV or aiohttp.
"""
import json
import os
import re
import struct
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cameras"))

import hand_cameras as hc  # noqa: E402

PORT_A = "platform-3610000.xhci-usb-0:2.3.3:1.0-video-index0"
PORT_B = "platform-3610000.xhci-usb-0:2.3.4:1.0-video-index0"
PORT_A_META = "platform-3610000.xhci-usb-0:2.3.3:1.0-video-index1"
PORT_RS = "platform-3610000.xhci-usb-0:2.1:1.0-video-index0"


def fake_tree(tmp_path, nodes):
    """nodes: [(by_path_name, videoN, sysfs name)] -> (by_path_dir, sys_dir)."""
    dev = tmp_path / "dev"
    by_path = dev / "v4l" / "by-path"
    sys_dir = tmp_path / "sys"
    by_path.mkdir(parents=True)
    for link, node, name in nodes:
        (dev / node).write_text("")
        (by_path / link).symlink_to(os.path.join("..", "..", node))
        (sys_dir / node).mkdir(parents=True, exist_ok=True)
        (sys_dir / node / "name").write_text(name + "\n")
    return str(by_path), str(sys_dir)


ROBOT = [(PORT_B, "video8", "Autodarts DIY Cam: Autodarts DI"),
         (PORT_A, "video6", "Autodarts DIY Cam: Autodarts DI"),
         (PORT_A_META, "video7", "Autodarts DIY Cam: Autodarts DI"),
         (PORT_RS, "video4", "Intel(R) RealSense(TM) Depth Ca")]


def test_usb_port():
    assert hc.usb_port("/dev/v4l/by-path/" + PORT_B) == "2.3.4"
    assert hc.usb_port("video6") is None


def test_capture_nodes_are_index0_sorted_by_port(tmp_path):
    by_path, sys_dir = fake_tree(tmp_path, ROBOT)
    nodes = hc.list_capture_nodes(by_path, sys_dir)
    assert [os.path.basename(n["by_path"]) for n in nodes] == [PORT_RS, PORT_A, PORT_B]
    assert [os.path.basename(n["dev"]) for n in nodes] == ["video4", "video6", "video8"]


def test_auto_skips_realsense_and_takes_port_order(tmp_path):
    by_path, sys_dir = fake_tree(tmp_path, ROBOT)
    nodes = hc.list_capture_nodes(by_path, sys_dir)
    got = hc.assign("auto", "auto", nodes)
    assert os.path.basename(got["left"]) == PORT_A
    assert os.path.basename(got["right"]) == PORT_B


def test_match_narrows_auto(tmp_path):
    by_path, sys_dir = fake_tree(tmp_path, ROBOT)
    nodes = hc.list_capture_nodes(by_path, sys_dir)
    assert hc.assign("auto", "auto", nodes, match="nothing-like-this") == {"left": None, "right": None}


def test_explicit_path_wins_and_auto_takes_the_rest(tmp_path):
    by_path, sys_dir = fake_tree(tmp_path, ROBOT)
    nodes = hc.list_capture_nodes(by_path, sys_dir)
    explicit = os.path.join(by_path, PORT_B)
    got = hc.assign(explicit, "auto", nodes)
    assert got["left"] == explicit
    assert os.path.basename(got["right"]) == PORT_A


def test_link_name_alone_means_by_path():
    assert hc.normalize(PORT_B) == os.path.join(hc.BY_PATH, PORT_B)
    assert hc.normalize("/dev/video6") == "/dev/video6"
    assert hc.normalize(" AUTO ") == "auto"


def test_saved_mapping_is_honoured_only_while_plugged_in(tmp_path):
    by_path, sys_dir = fake_tree(tmp_path, ROBOT)
    nodes = hc.list_capture_nodes(by_path, sys_dir)
    a, b = os.path.join(by_path, PORT_A), os.path.join(by_path, PORT_B)
    assert hc.assign("auto", "auto", nodes, saved={"left": b, "right": a}) == {"left": b, "right": a}
    gone = os.path.join(by_path, "platform-x-usb-0:9:1.0-video-index0")
    assert hc.assign("auto", "auto", nodes, saved={"left": gone, "right": a}) == {"left": b, "right": a}


class FakeCamera:
    def __init__(self, name, path, *a, **k):
        self.name, self.path = name, path
        self.started = False

    def start(self):
        self.started = True

    def status(self):
        return {"name": self.name, "path": self.path}

    def latest(self):
        return None


def env_for(tmp_path, **kw):
    env = {"HAND_CAMERAS": "on", "HAND_CAM_STATE_DIR": str(tmp_path / "state")}
    env.update(kw)
    return env


def test_swap_flips_and_is_remembered(tmp_path):
    by_path, sys_dir = fake_tree(tmp_path, ROBOT)
    env = env_for(tmp_path)
    cams = hc.HandCameras(env, by_path, sys_dir, camera_cls=FakeCamera)
    a, b = os.path.join(by_path, PORT_A), os.path.join(by_path, PORT_B)
    assert cams.get("left").path == a and cams.get("right").path == b
    ok, _ = cams.swap()
    assert ok
    assert cams.get("left").path == b and cams.get("left").name == "left"
    assert cams.get("right").path == a and cams.get("right").name == "right"
    saved = json.loads((tmp_path / "state" / "cameras.json").read_text())
    assert saved == {"left": b, "right": a}
    # A restart reads the saved choice back.
    again = hc.HandCameras(env, by_path, sys_dir, camera_cls=FakeCamera)
    assert again.get("left").path == b and again.get("right").path == a


def test_no_swap_when_set_explicitly(tmp_path):
    by_path, sys_dir = fake_tree(tmp_path, ROBOT)
    env = env_for(tmp_path, HAND_CAM_LEFT=PORT_B, HAND_CAM_RIGHT=PORT_A)
    cams = hc.HandCameras(env, by_path, sys_dir, camera_cls=FakeCamera)
    ok, msg = cams.swap()
    assert not ok and "explicitly" in msg
    assert not (tmp_path / "state" / "cameras.json").exists()


def test_off_opens_nothing(tmp_path):
    by_path, sys_dir = fake_tree(tmp_path, ROBOT)
    cams = hc.HandCameras(env_for(tmp_path, HAND_CAMERAS="off"), by_path, sys_dir,
                          camera_cls=FakeCamera)
    assert cams.cameras == {} and cams.status()["enabled"] is False


def tiny_jpeg(w=640, h=480):
    """SOI, an APP0 segment, SOF0 with the size, EOI: enough for jpeg_size."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, h, w, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def test_jpeg_size_reads_the_sof():
    assert hc.jpeg_size(tiny_jpeg(640, 480)) == (640, 480)
    assert hc.jpeg_size(b"\xff\xd8\xff\xd9") == (None, None)


class FakeCap:
    """A V4L2 capture returning the camera's own JPEG as one row of bytes."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.released = False

    def grab(self):
        return bool(self.frames)

    def retrieve(self):
        np = pytest.importorskip("numpy")
        data = self.frames.pop(0)
        return True, np.frombuffer(data, dtype=np.uint8).reshape(1, -1)

    def release(self):
        self.released = True


def test_frames_pass_through_untouched_and_stamped_on_the_monotonic_clock():
    pytest.importorskip("numpy")
    jpeg = tiny_jpeg(640, 480)
    cap = FakeCap([jpeg] * 3)
    cam = hc.HandCamera("left", "/dev/v4l/by-path/" + PORT_A, opener=lambda: cap,
                        reencode=lambda img: pytest.fail("must not re-encode"))
    before = time.monotonic_ns()
    cam.start()
    deadline = time.time() + 2
    while cam.seq < 3 and time.time() < deadline:
        time.sleep(0.01)
    cam.stop()
    header, data = cam.latest()
    assert data == jpeg
    assert header["cam"] == "left" and header["port"] == "2.3.3"
    assert (header["w"], header["h"]) == (640, 480)
    assert before <= header["t_cap_mono_ns"] <= time.monotonic_ns()
    assert cam.status()["reencoded"] == 0


def test_a_decoded_frame_is_encoded_again_and_counted():
    np = pytest.importorskip("numpy")
    cam = hc.HandCamera("right", "/x", opener=lambda: None, reencode=lambda img: b"\xff\xd8re")
    assert cam.to_jpeg(np.zeros((480, 640, 3), dtype=np.uint8)) == b"\xff\xd8re"
    assert cam.n_reencoded == 1
    with pytest.raises(RuntimeError):
        cam.to_jpeg(np.frombuffer(b"not a jpeg", dtype=np.uint8).reshape(1, -1))


def test_pack_frame_matches_the_bridge_unpacker():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))
    pytest.importorskip("aiohttp")
    from cam_client import unpack_frame
    header = {"cam": "left", "seq": 7}
    assert unpack_frame(hc.pack_frame(header, b"\xff\xd8jpeg")) == (header, b"\xff\xd8jpeg")


ROOT = os.path.join(os.path.dirname(__file__), "..")
# Spelled in pieces: `wendy init` renders every text file, and a literal
# placeholder here would be rewritten in the scaffolded project.
OPEN, CLOSE = "{" * 2, "}" * 2


def test_wendy_json_gives_the_cameras_service_the_camera_and_its_own_state():
    text = open(os.path.join(ROOT, "wendy.json")).read()
    text = re.sub(re.escape(OPEN) + r"\.APP_ID" + re.escape(CLOSE), "app", text)
    text = re.sub(re.escape(OPEN) + r"\.[A-Z_]+" + re.escape(CLOSE), "8443", text)
    svc = json.loads(text)["services"]["cameras"]
    types = {e["type"] for e in svc["entitlements"]}
    assert {"camera", "network", "persist"} <= types
    persist = [e for e in svc["entitlements"] if e["type"] == "persist"][0]
    assert persist["path"] == "/state" and persist["name"].endswith("-cameras")


def test_template_declares_the_camera_variables_the_dockerfile_uses():
    path = os.path.join(ROOT, "template.json")
    if not os.path.exists(path):
        pytest.skip("a scaffolded project has no template.json")
    names = {v["name"] for v in json.load(open(path))["variables"]}
    docker = open(os.path.join(ROOT, "cameras", "Dockerfile")).read()
    for var in ("HAND_CAMERAS", "LEFT_HAND_CAMERA", "RIGHT_HAND_CAMERA", "CAMERA_FPS"):
        assert var in names
        assert OPEN + "." + var + CLOSE in docker


def test_head_camera_none_turns_the_head_camera_off():
    pytest.importorskip("aiohttp")
    sys.path.insert(0, os.path.join(ROOT, "onboard"))
    from g1_camera import CameraSource
    assert CameraSource.from_env({"G1_CAM": "on", "G1_CAMERA": "none"}) is None
    assert CameraSource.from_env({"G1_CAM": "off", "G1_CAMERA": "4"}) is None
    assert CameraSource.from_env({"G1_CAM": "on", "G1_CAMERA": "4"}).device == "4"
