"""The head camera goes into every episode, or the episodes cannot train anything.

An episode records the operator's hands, the commands and the robot's joints.
A learned policy never sees the operator's hands; it sees what the robot sees.
Without the head camera, time-aligned with the joints, the recordings are good
for debugging teleop and useless for imitation learning -- which was the point
of recording them.

Shape of it: the ONBOARD owns the camera (it is on the robot), grabs frames at
a fixed rate, JPEG-encodes them and streams them on a second WebSocket, /cam.
The BRIDGE keeps the newest frame, hands each one to the session recorder, and
stamps every pose row with the sequence number and age of the camera frame
that was current when the row was written. The recorder writes frames beside
the session as files plus an index, never inline: 15 frames a second of JPEG
in a JSONL line would make the file unreadable for everything else.
"""
from __future__ import annotations

import asyncio
import json
import re
import os
import sys
import threading
import time

import numpy as np
import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "onboard"))
sys.path.insert(0, os.path.join(ROOT, "teleop", "bridge"))

import g1_camera as cam  # noqa: E402
import cam_client  # noqa: E402
from session_recorder import SessionRecorder  # noqa: E402


# ---- framing: two images, one wire format ------------------------------------

def test_a_frame_packed_on_the_robot_unpacks_on_the_bridge():
    header = {"seq": 7, "t": 1789431174.405, "w": 640, "h": 480, "fmt": "jpeg"}
    blob = cam.pack_frame(header, b"\xff\xd8jpegbytes\xff\xd9")
    got_header, got_jpeg = cam_client.unpack_frame(blob)
    assert got_header == header and got_jpeg == b"\xff\xd8jpegbytes\xff\xd9"


def test_garbage_is_refused_not_crashed():
    assert cam_client.unpack_frame(b"") is None
    assert cam_client.unpack_frame(b"\x00\x00\x00\x10short") is None


# ---- onboard capture ----------------------------------------------------------

class _FakeCap:
    """A camera that returns numbered images and can be told to fail."""
    def __init__(self, fail_reads=0):
        self.n = 0
        self.fail_reads = fail_reads
        self.released = False

    def read(self):
        self.n += 1
        if self.fail_reads:
            self.fail_reads -= 1
            return False, None
        img = np.full((4, 6, 3), self.n % 256, dtype=np.uint8)
        return True, img

    def release(self):
        self.released = True


def _fake_encoder(img, quality):
    return b"JPG" + bytes([int(img[0, 0, 0])])


def _source(hz=200.0, opener=None, **kw):
    return cam.CameraSource(device="0", width=6, height=4, hz=hz, quality=80,
                            opener=opener or (lambda: _FakeCap()),
                            encoder=_fake_encoder, **kw)


def _wait(pred, timeout=2.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.005)
    return False


def test_the_source_captures_encodes_and_numbers_frames():
    s = _source()
    s.start()
    try:
        assert _wait(lambda: s.n_captured >= 5)
        header, jpeg = s.latest()
        assert header["w"] == 6 and header["h"] == 4 and header["fmt"] == "jpeg"
        assert header["seq"] == s.n_captured
        assert jpeg.startswith(b"JPG")
        assert abs(header["t"] - time.time()) < 1.0
    finally:
        s.stop()


def test_the_source_is_paced_to_its_rate():
    s = _source(hz=50.0)
    s.start()
    try:
        time.sleep(0.5)
        n = s.n_captured
    finally:
        s.stop()
    assert 15 <= n <= 35, f"{n} frames in 0.5 s at 50 Hz"


def test_a_failing_read_is_counted_and_the_camera_reopened():
    caps = []

    def opener():
        c = _FakeCap(fail_reads=3 if not caps else 0)
        caps.append(c)
        return c
    s = _source(opener=opener, reopen_after=2, reopen_delay=0.01)
    s.start()
    try:
        assert _wait(lambda: len(caps) >= 2 and s.n_captured >= 3)
        assert caps[0].released, "the failed camera must be released before reopening"
        assert s.n_failed >= 2
    finally:
        s.stop()


def test_an_opener_that_raises_does_not_kill_the_thread():
    calls = []

    def opener():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("no such device")
        return _FakeCap()
    s = _source(opener=opener, reopen_delay=0.01)
    s.start()
    try:
        assert _wait(lambda: s.n_captured >= 1)
        assert "no such device" in (s.last_error or "") or s.last_error is None
        st = s.status()
        assert st["device"] == "0" and st["captured"] >= 1 and st["open_failures"] >= 2
    finally:
        s.stop()


def test_from_env_can_be_switched_off():
    assert cam.CameraSource.from_env({"G1_CAM": "0"}) is None
    s = cam.CameraSource.from_env({"G1_CAMERA": "/dev/video4", "G1_CAM_HZ": "10", "G1_CAM_W": "320", "G1_CAM_H": "240"},
                                  opener=lambda: _FakeCap(), encoder=_fake_encoder)
    assert s.device == "/dev/video4" and s.hz == 10.0 and (s.width, s.height) == (320, 240)


class _FakeWS:
    def __init__(self, n_before_close):
        self.sent = []
        self._n = n_before_close
        self.closed = False

    async def prepare(self, request):
        pass

    async def send_bytes(self, b):
        self.sent.append(b)
        if len(self.sent) >= self._n:
            self.closed = True

    async def send_json(self, m):
        self.sent.append(m)

    async def close(self):
        self.closed = True


def test_the_cam_socket_sends_each_new_frame_once_and_stops_when_closed():
    s = _source(hz=200.0)
    s.start()
    try:
        assert _wait(lambda: s.n_captured >= 2)
        ws = _FakeWS(n_before_close=5)
        asyncio.run(cam.stream_camera(ws, s, hz=200.0))
        seqs = [cam_client.unpack_frame(b)[0]["seq"] for b in ws.sent]
        assert len(seqs) == 5 and seqs == sorted(set(seqs)), seqs
    finally:
        s.stop()


def test_the_cam_socket_takes_the_key():
    import types
    app = {"secret": "k", "camera": _source()}
    req = types.SimpleNamespace(app=app, query={"key": "wrong"})
    ws = _FakeWS(1)
    out = asyncio.run(cam.cam_ws_handler(req, ws_factory=lambda: ws))
    assert ws.closed and ws.sent and ws.sent[0].get("error") == "bad key"


# ---- bridge client -------------------------------------------------------------

def _client(url="ws://robot:8090/cam?key=k", on_frame=None):
    c = cam_client.CameraClient.__new__(cam_client.CameraClient)
    c._init_state(url, on_frame)
    return c


def test_cam_url_is_derived_from_the_ctrl_url():
    assert cam_client.cam_url_from_ctrl("ws://10.10.20.238:8090/ctrl?key=abc") == "ws://10.10.20.238:8090/cam?key=abc"
    assert cam_client.cam_url_from_ctrl("ws://h:1/ctrl") == "ws://h:1/cam"


def test_the_client_keeps_the_newest_frame_and_hands_each_one_on():
    got = []
    c = _client(on_frame=lambda h, j: got.append(h["seq"]))
    c._handle(cam.pack_frame({"seq": 1, "t": 10.0, "w": 6, "h": 4, "fmt": "jpeg"}, b"a"))
    c._handle(cam.pack_frame({"seq": 2, "t": 10.1, "w": 6, "h": 4, "fmt": "jpeg"}, b"b"))
    ref = c.latest_ref()
    assert ref["seq"] == 2 and ref["t"] == 10.1 and ref["age_s"] < 1.0
    assert got == [1, 2]
    st = c.status()
    assert st["frames"] == 2 and st["w"] == 6 and st["h"] == 4 and st["bytes"] == 2


def test_the_client_is_honest_before_any_frame():
    c = _client()
    assert c.latest_ref() is None
    assert c.status()["frames"] == 0 and c.status()["connected"] is False


# ---- recorder -----------------------------------------------------------------

def _drain(r, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not r.status()["queued"]:
            return True
        time.sleep(0.01)
    return False


def test_camera_frames_are_written_beside_the_session_with_an_index(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    sid = rec.start("x")["session"]
    # One ARMED row first: the recorder keeps camera frames only while
    # the robot is ARMED, and these test the camera plumbing.
    rec.note({"hands": []}, {}, {"state": "ARMED"})
    rec.note_camera({"seq": 1, "t": 100.0, "w": 6, "h": 4, "fmt": "jpeg"}, b"\xff\xd8one")
    rec.note_camera({"seq": 2, "t": 100.066, "w": 6, "h": 4, "fmt": "jpeg"}, b"\xff\xd8two")
    rec.note({"t": 100.07, "hands": []}, {"left_elbow_joint": 0.1}, {"state": "ARMED"},
             cam={"seq": 2, "t": 100.066, "age_s": 0.004})
    assert _drain(rec)
    out = rec.stop()
    frames_dir = tmp_path / f"{sid}.frames"
    assert (frames_dir / "000001.jpg").read_bytes() == b"\xff\xd8one"
    assert (frames_dir / "000002.jpg").read_bytes() == b"\xff\xd8two"
    index = [json.loads(l) for l in open(frames_dir / "index.jsonl")]
    assert [i["seq"] for i in index] == [1, 2]
    assert index[0]["file"] == "000001.jpg" and index[0]["t"] == 100.0 and index[0]["bytes"] == 5
    rows = [json.loads(l) for l in open(tmp_path / f"{sid}.jsonl")]
    body = [r for r in rows if r.get("type") not in ("header", "footer")]
    # The row that carries a cam reference, not a fixed index: an ARMED
    # row now precedes the frames, because that is what opens the gate.
    cam_rows = [r for r in body if "cam" in r]
    assert cam_rows and cam_rows[0]["cam"] == {"seq": 2, "t": 100.066, "age_s": 0.004}
    footer = rows[-1]
    assert footer["type"] == "footer" and footer["cam_frames"] == 2 and footer["cam_dropped"] == 0
    assert out["cam_frames"] == 2


def test_no_camera_means_no_frames_directory(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    sid = rec.start("x")["session"]
    rec.note({"t": 1.0, "hands": []}, {"left_elbow_joint": 0.1}, {"state": "ARMED"})
    assert _drain(rec)
    rec.stop()
    assert not (tmp_path / f"{sid}.frames").exists()


def test_camera_frames_outside_a_session_are_ignored(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    # One ARMED row first: the recorder keeps camera frames only while
    # the robot is ARMED, and these test the camera plumbing.
    rec.note({"hands": []}, {}, {"state": "ARMED"})
    rec.note_camera({"seq": 1, "t": 1.0, "w": 6, "h": 4, "fmt": "jpeg"}, b"x")
    time.sleep(0.05)
    assert not list(tmp_path.iterdir())


def test_a_backed_up_queue_drops_camera_frames_and_says_so(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    rec.start("x")
    # ARMED before the writer is stopped: otherwise the frames are SKIPPED
    # (the recorder declining to film an empty table) and never reach the
    # queue at all, so this would pass while testing nothing.
    rec.note({"hands": []}, {}, {"state": "ARMED"})
    rec._stop = True                      # stop the writer so the queue fills
    rec._wake.set()
    rec._thread.join(1.0)
    for i in range(SessionRecorder.QUEUE_MAX + 5):
        rec.note_camera({"seq": i, "t": 1.0, "w": 6, "h": 4, "fmt": "jpeg"}, b"x")
    assert rec.n_cam_dropped == 5
    assert rec.status()["cam_dropped"] == 5


# ---- deployment ----------------------------------------------------------------

def test_the_onboard_asks_for_the_video_entitlement_and_ships_opencv():
    text = open(os.path.join(ROOT, "wendy.json")).read()
    # Before `wendy init` the manifest still has template placeholders.
    w = json.loads(re.sub(r"\{\{[^}]*\}\}", "0", text))
    w = w["services"]["onboard"]
    # "camera", not "video": the CLI warns that "video" is deprecated, and a
    # deprecated entitlement is one that stops being granted at some release
    # nobody here chooses. Caught by the deploy printing the warning five
    # times while failing for an unrelated reason.
    assert {"type": "camera"} in w["entitlements"], \
        "the camera entitlement must not be the deprecated spelling"
    df = open(os.path.join(ROOT, "onboard", "Dockerfile")).read()
    assert "opencv-python-headless" in df


# ---- replay --------------------------------------------------------------------
