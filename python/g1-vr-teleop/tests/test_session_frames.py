"""The camera frames must be retrievable, or they are not really recorded.

They live in <sid>.frames/ inside the bridge's CONTAINER, and the container is
replaced on every deploy. /session/get returned the .jsonl alone, so an episode
could be pulled, committed and sealed while the images its rows point at stayed
behind to be deleted by the next redeploy -- which is the entire reason the
camera was added. The seal notices (`camera frames 0`) but only afterwards.
"""
import asyncio
import io
import json
import os
import pathlib
import sys
import tarfile
import types

import pytest

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))
sys.path.insert(0, str(ROOT / "onboard"))
import server                                                    # noqa: E402


class _Rec:
    def __init__(self, path): self._path = path
    def path(self, sid): return self._path if sid == "S" else None


def _req(app, match, query=None):
    return types.SimpleNamespace(app=app, match_info=match, query=query or {},
                                 method="GET", headers={})


def _session(tmp_path, n_frames=3, gz=False):
    name = "S.jsonl.gz" if gz else "S.jsonl"
    p = tmp_path / name
    p.write_bytes(b"{}\n")
    d = tmp_path / "S.frames"
    d.mkdir()
    (d / "index.jsonl").write_text('{"seq":1}\n')
    for i in range(n_frames):
        (d / f"{i}.jpg").write_bytes(b"\xff\xd8\xff" + bytes([i]) * 40)
    return str(p)


def _app(path):
    return {"session": _Rec(path), "control_key": None}


def test_the_frames_come_back_as_a_tar(tmp_path):
    app = _app(_session(tmp_path))
    resp = asyncio.run(server.session_handler(_req(app, {"step": "frames"}, {"session": "S"})))
    assert resp.content_type == "application/x-tar"
    with tarfile.open(fileobj=io.BytesIO(resp.body)) as tf:
        names = sorted(os.path.basename(n) for n in tf.getnames() if n.endswith((".jpg", ".jsonl")))
    assert names == ["0.jpg", "1.jpg", "2.jpg", "index.jsonl"]


def test_the_images_survive_the_round_trip_byte_for_byte(tmp_path):
    """A tar that mangles a JPEG is worse than no endpoint: it would be sealed."""
    path = _session(tmp_path)
    app = _app(path)
    resp = asyncio.run(server.session_handler(_req(app, {"step": "frames"}, {"session": "S"})))
    with tarfile.open(fileobj=io.BytesIO(resp.body)) as tf:
        m = next(n for n in tf.getnames() if n.endswith("1.jpg"))
        got = tf.extractfile(m).read()
    assert got == (tmp_path / "S.frames" / "1.jpg").read_bytes()


def test_a_gzipped_session_finds_its_frames_too(tmp_path):
    """Episodes are stored gzipped; the directory name has no .gz."""
    app = _app(_session(tmp_path, gz=True))
    resp = asyncio.run(server.session_handler(_req(app, {"step": "frames"}, {"session": "S"})))
    assert resp.content_type == "application/x-tar"


def test_an_unknown_session_is_404_not_an_empty_tar(tmp_path):
    app = _app(_session(tmp_path))
    resp = asyncio.run(server.session_handler(_req(app, {"step": "frames"}, {"session": "nope"})))
    assert resp.status == 404


def test_a_session_with_no_camera_says_so_rather_than_returning_nothing(tmp_path):
    """An empty tar would look like "recorded, no frames"; this is "none here"."""
    p = tmp_path / "S.jsonl"
    p.write_bytes(b"{}\n")
    resp = asyncio.run(server.session_handler(_req(_app(str(p)), {"step": "frames"},
                                                   {"session": "S"})))
    assert resp.status == 404
    assert "no camera frames" in json.loads(resp.text)["error"]


def test_it_is_gated_by_the_control_key_exactly_like_get(tmp_path):
    """Whatever the policy is, the frames must share it with the .jsonl.

    They are two halves of one recording; a tighter gate on one is a false
    sense of protection and a looser one is a hole."""
    app = _app(_session(tmp_path))
    app["control_key"] = "secret"
    frames = asyncio.run(server.session_handler(_req(app, {"step": "frames"}, {"session": "S"})))
    jsonl = asyncio.run(server.session_handler(_req(app, {"step": "get"}, {"session": "S"})))
    assert getattr(frames, "status", 200) == getattr(jsonl, "status", 200)
