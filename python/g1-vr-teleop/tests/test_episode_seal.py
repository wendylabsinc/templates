"""A recording is training data only once it is sealed.

The coke demo pipeline treats hashes as gates: a stage consumes the previous
stage's artifact only if every digest in its manifest still matches. Until now
an episode here was a file and a README paragraph, and nothing said whether the
file had been touched since the run. `bridge/episode_seal.py` writes one
manifest beside each session -- schema `wendy.g1.real-demonstration.v1`,
episode digest, camera frame digests, the header verbatim, the counts a trainer
selects on, and the validator's verdict -- and `verify` fails closed on any
byte that has moved since.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "teleop", "bridge"))

import episode_check  # noqa: E402
import episode_seal  # noqa: E402
import server  # noqa: E402
from session_recorder import SessionRecorder  # noqa: E402
from tests.test_endpoint_gating import _req, KEY  # noqa: E402



# ---- fixtures -----------------------------------------------------------------

def _frame(t, armed=True, cam=None, can=None, wrist_ref=None):
    row = {"t": t, "hands": [], "head": None,
           "robot": {"state": "ARMED" if armed else "HOLDING", "q": [0.0] * 14,
                     "q_age_s": 0.01, "applied": True},
           "targets": {"left_elbow_joint": 0.1, "left_wrist_roll_joint": 0.0,
                       "right_elbow_joint": 0.2}}
    if cam is not None:
        row["cam"] = cam
    if can is not None:
        row["can"] = can
    if wrist_ref is not None:
        row["wrist_ref"] = wrist_ref
    return row


def _write_session(tmp_path, sid="20260916-101010-seal", gz=False, frames_dir=False,
                   outcomes=False, can=False):
    """Two marked attempts (1 s ARMED, 2 s ARMED), a 5 s pause, 0.5 s HOLDING."""
    name = f"{sid}.jsonl" + (".gz" if gz else "")
    p = tmp_path / name
    op = gzip.open if gz else open
    cam_ref = {"seq": 1, "t": 100.0, "age_s": 0.01} if frames_dir else None
    with op(p, "wt") as fh:
        fh.write(json.dumps({"type": "header", "session": sid, "t_start": 100.0, "v": 1,
                             "arm_mode": "geometry",
                             "config": {"arm_mode": "geometry", "shoulder": True}}) + "\n")
        t = 100.0
        fh.write(json.dumps({"type": "mark", "t": t, "label": "attempt 1"}) + "\n")
        for i in range(90):
            fh.write(json.dumps(_frame(t + i / 90, cam=cam_ref,
                                       can={"status": "tracking"} if can else None,
                                       wrist_ref={"v": 1} if i == 0 else None)) + "\n")
        t += 1.0
        if outcomes:
            fh.write(json.dumps({"type": "outcome", "t": t, "result": "success",
                                 "phase": "release"}) + "\n")
        fh.write(json.dumps({"type": "mark", "t": t, "label": "attempt 2"}) + "\n")
        for i in range(180):
            fh.write(json.dumps(_frame(t + i / 90, cam=cam_ref)) + "\n")
        t += 2.0
        if outcomes:
            fh.write(json.dumps({"type": "outcome", "t": t, "result": "fail",
                                 "phase": "grasp"}) + "\n")
        t += 5.0
        for i in range(45):
            fh.write(json.dumps(_frame(t + i / 90, armed=False, cam=cam_ref)) + "\n")
        fh.write(json.dumps({"type": "footer", "frames": 315, "dropped": 0, "marks": 2,
                             "cam_frames": 3 if frames_dir else 0, "cam_dropped": 0,
                             "t_end": t + 1}) + "\n")
    if frames_dir:
        d = tmp_path / f"{sid}.frames"
        d.mkdir()
        with open(d / "index.jsonl", "w") as idx:
            for seq in (1, 2, 3):
                jpg = bytes([0xFF, 0xD8, seq]) * 100
                with open(d / f"{seq:06d}.jpg", "wb") as fh:
                    fh.write(jpg)
                idx.write(json.dumps({"seq": seq, "t": 100.0 + seq, "t_recv": 100.01 + seq,
                                      "bytes": len(jpg), "w": 640, "h": 480,
                                      "file": f"{seq:06d}.jpg"}) + "\n")
    return str(p)


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---- seal ---------------------------------------------------------------------

def test_seal_writes_a_manifest_beside_the_session_with_the_schema_and_digest(tmp_path):
    p = _write_session(tmp_path)
    m = episode_seal.seal(p)
    mp = episode_seal.manifest_path(p)
    assert mp == str(tmp_path / "20260916-101010-seal.manifest.json")
    assert os.path.exists(mp)
    assert m["schema"] == "wendy.g1.real-demonstration.v1"
    assert m["session"] == "20260916-101010-seal"
    assert m["episode"] == {"file": "20260916-101010-seal.jsonl", "bytes": os.path.getsize(p),
                            "sha256": _sha(p), "gzip": False}
    assert isinstance(m["sealed_at"], float) and m["sealed_at"] > 1.7e9
    assert m["source"]["repo"] == "g1-vr-teleop"
    assert set(m["source"]) == {"repo", "commit", "dirty"}
    # The header verbatim: this is what makes the run reproducible.
    assert m["header"]["config"] == {"arm_mode": "geometry", "shoulder": True}
    assert m["footer"]["frames"] == 315
    assert json.load(open(mp)) == m


def test_the_counts_are_what_a_trainer_selects_on(tmp_path):
    p = _write_session(tmp_path, outcomes=True, can=True)
    m = episode_seal.seal(p)
    c = m["counts"]
    assert c["frames"] == 315
    assert c["armed_frames"] == 270
    assert c["marks"] == 2
    assert c["outcomes"] == {"success": 1, "fail": 1}
    assert [a["label"] for a in c["attempts"]] == ["attempt 1", "attempt 2", "after pause"]
    assert [a["frames"] for a in c["attempts"]] == [90, 180, 45]
    assert c["attempts"][0]["t0"] == pytest.approx(100.0)
    assert c["attempts"][1]["seconds"] == pytest.approx(2.0, abs=0.05)
    assert c["attempts"][1]["armed_frac"] == pytest.approx(1.0)
    assert m["duration_s"] == pytest.approx(8.5, abs=0.05)
    assert m["joints"] == ["left_elbow_joint", "left_wrist_roll_joint", "right_elbow_joint"]
    assert m["has_measured_q"] is True
    assert m["has_wrist_ref"] is True
    assert m["has_can"] is True
    assert m["camera"] is None
    assert m["check"]["frames"] == 315 and "ok" in m["check"]


def test_a_session_without_outcomes_or_can_says_so_rather_than_failing(tmp_path):
    m = episode_seal.seal(_write_session(tmp_path))
    assert m["counts"]["outcomes"] == {}
    assert m["has_can"] is False


def test_gzip_sessions_seal_the_same_way(tmp_path):
    p = _write_session(tmp_path, gz=True)
    m = episode_seal.seal(p)
    assert m["episode"]["gzip"] is True
    assert m["episode"]["file"].endswith(".jsonl.gz")
    assert m["episode"]["sha256"] == _sha(p)
    assert episode_seal.manifest_path(p).endswith("20260916-101010-seal.manifest.json")
    assert m["counts"]["frames"] == 315


def test_the_camera_frames_are_sealed_one_digest_per_file(tmp_path):
    p = _write_session(tmp_path, frames_dir=True)
    m = episode_seal.seal(p)
    cam = m["camera"]
    d = tmp_path / "20260916-101010-seal.frames"
    assert cam["dir"] == "20260916-101010-seal.frames"
    assert cam["frames"] == 3
    assert cam["index_sha256"] == _sha(d / "index.jsonl")
    lines = sorted(f"{n}:{_sha(d / n)}" for n in os.listdir(d) if n.endswith(".jpg"))
    assert cam["frames_sha256"] == hashlib.sha256("\n".join(lines).encode()).hexdigest()


# ---- immutability -------------------------------------------------------------

def test_resealing_an_unchanged_session_is_a_no_op_that_returns_the_same_manifest(tmp_path):
    p = _write_session(tmp_path)
    first = episode_seal.seal(p)
    mp = episode_seal.manifest_path(p)
    mtime = os.path.getmtime(mp)
    time.sleep(0.02)
    second = episode_seal.seal(p)
    assert second == first, "the original sealed_at must survive: a reseal is not a new seal"
    assert os.path.getmtime(mp) == mtime


def test_sealing_a_changed_session_over_its_manifest_is_refused(tmp_path):
    p = _write_session(tmp_path)
    episode_seal.seal(p)
    with open(p, "a") as fh:
        fh.write(json.dumps(_frame(999.0)) + "\n")
    with pytest.raises(episode_seal.SealRefused):
        episode_seal.seal(p)
    # And the manifest on disk is still the original.
    assert episode_seal.verify(episode_seal.manifest_path(p))["ok"] is False


# ---- verify -------------------------------------------------------------------

def test_verify_passes_a_sealed_session_with_and_without_frames(tmp_path):
    for frames in (False, True):
        sid = f"20260916-10101{int(frames)}-v"
        p = _write_session(tmp_path, sid=sid, frames_dir=frames)
        episode_seal.seal(p)
        rep = episode_seal.verify(episode_seal.manifest_path(p))
        assert rep["ok"] is True and rep["reasons"] == [], rep
        assert rep["session"] == sid
        assert rep["episode_sha256"] == _sha(p)


def test_one_flipped_byte_in_the_episode_fails_verify_and_names_it(tmp_path):
    p = _write_session(tmp_path)
    episode_seal.seal(p)
    data = bytearray(open(p, "rb").read())
    data[len(data) // 2] ^= 0x01
    open(p, "wb").write(data)
    rep = episode_seal.verify(episode_seal.manifest_path(p))
    assert rep["ok"] is False
    assert any("episode" in r and "sha256" in r for r in rep["reasons"]), rep["reasons"]


def test_a_missing_episode_fails_closed(tmp_path):
    p = _write_session(tmp_path)
    episode_seal.seal(p)
    os.remove(p)
    rep = episode_seal.verify(episode_seal.manifest_path(p))
    assert rep["ok"] is False and any("missing" in r for r in rep["reasons"])


def test_an_altered_or_deleted_camera_frame_fails_verify(tmp_path):
    p = _write_session(tmp_path, frames_dir=True)
    episode_seal.seal(p)
    d = tmp_path / "20260916-101010-seal.frames"
    mp = episode_seal.manifest_path(p)
    # altered
    with open(d / "000002.jpg", "ab") as fh:
        fh.write(b"x")
    rep = episode_seal.verify(mp)
    assert rep["ok"] is False and any("frames" in r for r in rep["reasons"]), rep["reasons"]
    # deleted
    os.remove(d / "000002.jpg")
    rep = episode_seal.verify(mp)
    assert rep["ok"] is False and any("frames" in r for r in rep["reasons"]), rep["reasons"]


def test_an_altered_index_fails_verify(tmp_path):
    p = _write_session(tmp_path, frames_dir=True)
    episode_seal.seal(p)
    d = tmp_path / "20260916-101010-seal.frames"
    with open(d / "index.jsonl", "a") as fh:
        fh.write("\n")
    rep = episode_seal.verify(episode_seal.manifest_path(p))
    assert rep["ok"] is False and any("index" in r for r in rep["reasons"]), rep["reasons"]


def test_a_manifest_whose_header_no_longer_matches_fails_verify(tmp_path):
    p = _write_session(tmp_path)
    episode_seal.seal(p)
    mp = episode_seal.manifest_path(p)
    m = json.load(open(mp))
    m["header"]["config"]["shoulder"] = False
    json.dump(m, open(mp, "w"))
    rep = episode_seal.verify(mp)
    assert rep["ok"] is False and any("header" in r for r in rep["reasons"]), rep["reasons"]


def test_a_manifest_with_the_wrong_schema_is_not_ours(tmp_path):
    p = _write_session(tmp_path)
    episode_seal.seal(p)
    mp = episode_seal.manifest_path(p)
    m = json.load(open(mp))
    m["schema"] = "wendy.g1.something-else.v9"
    json.dump(m, open(mp, "w"))
    rep = episode_seal.verify(mp)
    assert rep["ok"] is False and any("schema" in r for r in rep["reasons"])


# ---- the bridge seals at stop -------------------------------------------------

def _drain(r, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not r.status()["queued"]:
            return True
        time.sleep(0.01)
    return False


def test_session_stop_seals_the_file_and_reports_the_manifest(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    sid = rec.start("x")["session"]
    for i in range(3):
        rec.note({"t": 1.0 + i / 90, "hands": []}, {"left_elbow_joint": 0.1},
                 {"state": "ARMED", "q": [0.0] * 14, "q_age_s": 0.01, "applied": True})
    assert _drain(rec)
    app = {"control_key": KEY, "session": rec, "retargeter": object(), "stats": {},
           "sink": server.LogSink()}
    resp = asyncio.run(server.session_handler(_req(app, {"step": "stop"}, key=KEY)))
    out = json.loads(resp.text)
    assert out["ok"] is True
    assert "check" in out, "the stop still validates"
    man = out["manifest"]
    mp = str(tmp_path / f"{sid}.manifest.json")
    assert man["path"] == mp and os.path.exists(mp)
    assert man["schema"] == "wendy.g1.real-demonstration.v1"
    assert man["sha256"] == _sha(tmp_path / f"{sid}.jsonl")
    assert episode_seal.verify(mp)["ok"] is True


def test_the_session_listing_says_which_sessions_are_sealed(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    a = rec.start("a")["session"]
    rec.stop()
    time.sleep(1.05)                       # session ids are per-second
    b = rec.start("b")["session"]
    rec.stop()
    episode_seal.seal(rec.path(a))
    listing = {s["session"]: s for s in rec.sessions()}
    assert listing[a]["manifest"] is True
    assert listing[b]["manifest"] is False


def test_session_manifest_endpoint_serves_the_sealed_file_and_is_gated(tmp_path):
    rec = SessionRecorder(str(tmp_path))
    sid = rec.start("m")["session"]
    rec.stop()
    episode_seal.seal(rec.path(sid))
    app = {"control_key": KEY, "session": rec, "retargeter": object(), "stats": {},
           "sink": server.LogSink()}
    resp = asyncio.run(server.session_handler(_req(app, {"step": "manifest"}, key=KEY,
                                                   query={"session": sid})))
    assert resp.status == 200
    assert str(getattr(resp, "_path", "")).endswith(f"{sid}.manifest.json")
    resp = asyncio.run(server.session_handler(_req(app, {"step": "manifest"}, key=KEY,
                                                   query={"session": "nope"})))
    assert resp.status == 404
    resp = asyncio.run(server.session_handler(_req(app, {"step": "manifest"},
                                                   query={"session": sid})))
    assert resp.status == 403


# ---- the attempts helper gained t0 -------------------------------------------

def test_episode_check_attempts_carry_t0(tmp_path):
    # five, not four: load() gained `outcomes` when attempt outcomes landed.
    header, body, footer, marks, outcomes = episode_check.load(_write_session(tmp_path))
    att = episode_check.attempts(body, marks)
    assert [a["t0"] for a in att] == pytest.approx([100.0, 101.0, 108.0], abs=0.02)
