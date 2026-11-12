"""Hand-camera frames are recorded beside the session, sealed, and checked.

A recording is only training data if its wrist views can be found, joined to
the joints, and trusted later. These tests drive the real recorder, seal and
validator with fake frames: no camera, no robot.
"""
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))

import episode_check  # noqa: E402
import episode_seal  # noqa: E402
from session_recorder import SessionRecorder  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 64 + b"\xff\xd9"
ARMED = {"state": "ARMED"}


def _wait_written(rec, rows, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if rec.status()["frames"] >= rows and not rec.status()["queued"]:
            return
        time.sleep(0.01)


def _hand_header(seq, t_mono, name="left"):
    return {"cam": name, "seq": seq, "t_cap_mono_ns": t_mono,
            "t_cap_unix_ns": time.time_ns(), "w": 640, "h": 480, "port": "2.3.3"}


def record(tmp_path, *, armed=True, cams=("left", "right"), n=5, header_meta=None):
    rec = SessionRecorder(str(tmp_path))
    out = rec.start("t", meta=header_meta or {"hand_cameras": {c: {"port": "2.3.3"} for c in cams}})
    sid = out["session"]
    for i in range(n):
        t = time.monotonic_ns()
        refs = {c: {"seq": i + 1, "t_cap_mono_ns": t, "age_s": 0.01} for c in cams}
        rec.note({"hands": [], "head": None}, robot=ARMED if armed else {"state": "HOLDING"},
                 hand_cams=refs)
        for c in cams:
            rec.note_hand_camera(c, _hand_header(100 + i, t + i * 33_000_000, c), JPEG)
    _wait_written(rec, n)
    stop = rec.stop()
    rec.close()
    return sid, stop, os.path.join(str(tmp_path), sid + ".jsonl")


def _rows(path):
    with open(path) as fh:
        return [json.loads(l) for l in fh if l.strip()]


def test_frames_land_per_camera_numbered_from_one(tmp_path):
    sid, stop, path = record(tmp_path)
    assert stop["hand_cams"]["left"]["frames"] == 5
    assert stop["hand_cams"]["right"]["frames"] == 5
    for cam in ("left", "right"):
        d = tmp_path / f"{sid}.frames" / f"hand-{cam}"
        assert sorted(p.name for p in d.glob("*.jpg")) == [f"{i:06d}.jpg" for i in range(1, 6)]
        assert (d / "000003.jpg").read_bytes() == JPEG
        idx = [json.loads(l) for l in (d / "index.jsonl").read_text().splitlines()]
        assert [r["seq"] for r in idx] == [1, 2, 3, 4, 5]
        assert [r["source_seq"] for r in idx] == [100, 101, 102, 103, 104]
        assert all(isinstance(r["t_cap_mono_ns"], int) and isinstance(r["t_recv_mono_ns"], int)
                   for r in idx)
        assert idx[0]["port"] == "2.3.3" and idx[0]["file"] == "000001.jpg"


def test_rows_carry_the_monotonic_clock_and_the_current_hand_frames(tmp_path):
    _, _, path = record(tmp_path)
    rows = [r for r in _rows(path) if r.get("type") not in ("header", "footer")]
    assert len(rows) == 5
    assert all(isinstance(r["t_mono_ns"], int) for r in rows)
    assert set(rows[0]["hand_cams"]) == {"left", "right"}
    footer = _rows(path)[-1]
    assert footer["type"] == "footer"
    assert footer["hand_cams"]["left"] == {"frames": 5, "dropped": 0, "skipped_not_armed": 0}


def test_frames_outside_armed_are_skipped_not_dropped(tmp_path):
    sid, stop, _ = record(tmp_path, armed=False)
    assert stop["hand_cams"]["left"]["frames"] == 0
    assert stop["hand_cams"]["left"]["skipped_not_armed"] == 5
    assert stop["hand_cams"]["left"]["dropped"] == 0
    assert not (tmp_path / f"{sid}.frames" / "hand-left").exists()


def test_a_name_that_could_escape_the_session_directory_is_ignored(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    rec.start("t")
    rec.note({"hands": []}, robot=ARMED)
    rec.note_hand_camera("../evil", _hand_header(1, time.monotonic_ns()), JPEG)
    _wait_written(rec, 1)
    stop = rec.stop()
    rec.close()
    assert "../evil" not in stop["hand_cams"]
    assert not any(p.name == "evil" for p in tmp_path.rglob("*"))


def test_seal_locks_the_hand_frames_and_verify_catches_a_change(tmp_path):
    sid, _, path = record(tmp_path)
    m = episode_seal.seal(path)
    assert set(m["hand_cameras"]) == {"left", "right"}
    assert m["hand_cameras"]["left"]["frames"] == 5
    assert m["hand_cameras"]["left"]["dir"] == f"{sid}.frames/hand-left"
    rep = episode_seal.verify(episode_seal.manifest_path(path))
    assert rep["ok"], rep["reasons"]
    # Resealing an unchanged session returns the same manifest.
    assert episode_seal.seal(path) == m
    (tmp_path / f"{sid}.frames" / "hand-right" / "000002.jpg").write_bytes(b"\xff\xd8edited")
    rep = episode_seal.verify(episode_seal.manifest_path(path))
    assert not rep["ok"]
    assert any("right hand camera frames sha256 differs" in r for r in rep["reasons"])


def test_check_reports_each_camera(tmp_path):
    _, _, path = record(tmp_path)
    rep = episode_check.check(path)
    left = rep["hand_cameras"]["left"]
    assert left["frames"] == 5 and left["armed_rows"] == 5 and left["armed_rows_fresh"] == 5
    assert left["fps"] is not None and 29 <= left["fps"] <= 31   # frames 33 ms apart
    assert left["max_gap_s"] is not None
    assert not [i for i in rep["issues"] if i["code"].startswith("hand_cam")]


def test_check_warns_when_an_expected_camera_wrote_nothing(tmp_path):
    _, _, path = record(tmp_path, cams=("left",),
                        header_meta={"hand_cameras": {"left": {}, "right": {}}})
    rep = episode_check.check(path)
    codes = [(i["code"], i["detail"]) for i in rep["issues"]]
    assert any(c == "hand_cam_empty" and "right" in d for c, d in codes)


def test_a_session_without_hand_cameras_is_unchanged(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    sid = rec.start("t")["session"]
    rec.note({"hands": []}, robot=ARMED)
    _wait_written(rec, 1)
    rec.stop()
    rec.close()
    path = os.path.join(str(tmp_path), sid + ".jsonl")
    assert episode_check.check(path)["hand_cameras"] == {}
    assert episode_seal.build(path)["hand_cameras"] is None
