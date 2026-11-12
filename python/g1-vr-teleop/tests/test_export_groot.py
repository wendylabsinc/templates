"""A sealed session becomes a GR00T / LeRobot v2.1 dataset, or nothing at all.

GR00T fine-tunes on "GR00T LeRobot": the LeRobot v2.1 layout (parquet per
episode, mp4 per camera, meta/*.jsonl) plus meta/modality.json naming the
slices of the concatenated state and action vectors. LeRobot's own v2.1 ->
v3.0 converter also needs meta/episodes_stats.jsonl. Ported from the
conference build (g1-vr-teleop, tag avangrid-conference).

The fixture is a synthetic session written the way the recorder writes one,
sealed the way the producer seals one, with real JPEGs so the video path runs
through ffmpeg and is read back through ffprobe. Every refusal rule has a
fixture that violates exactly one thing.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import re
import tarfile

import numpy as np
import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "teleop", "bridge"))

import episode_seal  # noqa: E402
import groot_export as ge  # noqa: E402
import offline_labels  # noqa: E402
from groot_export import ARM_LEFT, ARM_RIGHT, DEX3_LEFT, DEX3_RIGHT  # noqa: E402

try:
    from PIL import Image
except ImportError:  # pragma: no cover - environment dependent
    Image = None

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover - environment dependent
    pa = pq = None
HAVE_DEPS = Image is not None and pa is not None and pq is not None
HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_tools = pytest.mark.skipif(not (HAVE_DEPS and HAVE_FFMPEG),
                                 reason="needs pyarrow, Pillow, ffmpeg and ffprobe")

ARM = ARM_LEFT + ARM_RIGHT
HAND = DEX3_LEFT + DEX3_RIGHT
TASK = "Pick up the Coke can and place it in the receiver"
T0 = 1_700_000_000.0
CLI = os.path.join(ROOT, "tools", "export_dataset.py")
PY = sys.executable


def _export(*args, **kwargs):
    return ge.export(*args, **kwargs)


# ---- fixtures -----------------------------------------------------------------

def _body(waist_q):
    idx = list(range(12, 29))
    return {"idx": idx, "q": [waist_q, 0.0, 0.0] + [0.0] * 14, "dq": [0.0] * 17,
            "tau": [0.0] * 17, "age_s": 0.0}


def _rows(n=300, hz=90.0, cam_hz=15.0, elbow_lag_s=0.0, with_sent=True, with_hands=True,
          t_start=T0, waist=0.0, cam_gap=None, armed=True):
    """The left elbow command ramps 0 -> 1 rad over 2 s and the measured elbow
    follows (lagging elbow_lag_s); a camera frame every 1/cam_hz s from
    t_start; `cam_gap` = (from_s, to_s) window with no new frames."""
    rows, index = [], []
    seq, next_cam = 0, t_start
    for i in range(n):
        t = t_start + i / hz
        while next_cam <= t + 1e-9:
            if not (cam_gap and cam_gap[0] <= next_cam - t_start < cam_gap[1]):
                seq += 1
                index.append({"seq": seq, "t": round(next_cam, 4)})
            next_cam += 1.0 / cam_hz
        cmd = {j: 0.0 for j in ARM}
        cmd["left_elbow_joint"] = min(1.0, (t - t_start) / 2.0)
        targets = dict(cmd)
        targets["waist_yaw_joint"] = waist
        for j in HAND:
            targets[j] = 0.5
        q = [0.0] * 14
        q[ARM.index("left_elbow_joint")] = min(1.0, max(0.0, (t - elbow_lag_s - t_start) / 2.0))
        robot = {"state": "ARMED" if armed else "HOLDING", "q": q, "q_age_s": 0.0,
                 "applied": True, "body": _body(waist)}
        if with_hands:
            robot["hand_q"] = {"left": [0.1] * 7, "right": [0.2] * 7}
        row = {"t": t, "hands": [], "head": None, "robot": robot, "targets": targets}
        if with_sent:
            row["sent"] = dict(cmd, waist_yaw_joint=waist)
        if index:
            row["cam"] = {"seq": seq, "t": index[-1]["t"], "age_s": round(t - index[-1]["t"], 4)}
        rows.append(row)
    return rows, index


def _success_at(t):
    return {"type": "outcome", "t": t, "result": "success", "phase": "place"}


def _write_session(base, sid, rows, index=None, gz=True, header_extra=None, size=(64, 48)):
    """Write a session file and its frames dir the way the recorder does, then
    seal it the way the producer does. -> manifest path."""
    header = {"type": "header", "session": sid, "t_start": T0, "v": 1, "arm_mode": "geometry",
              "task": TASK,
              "config": {"gains": {"left_elbow_joint": {"kp": 110.0, "kd": 4.0}}}}
    header.update(header_extra or {})
    n_frames = sum(1 for r in rows if not r.get("type"))
    footer = {"type": "footer", "frames": n_frames, "dropped": 0, "marks": 0,
              "cam_frames": len(index or []), "cam_dropped": 0, "t_end": T0 + 100}
    lines = [json.dumps(header)] + [json.dumps(r) for r in rows] + [json.dumps(footer)]
    name = f"{sid}.jsonl" + (".gz" if gz else "")
    path = os.path.join(base, name)
    data = ("\n".join(lines) + "\n").encode()
    if gz:
        with gzip.open(path, "wb") as fh:
            fh.write(data)
    else:
        with open(path, "wb") as fh:
            fh.write(data)
    if index is not None:
        _write_frames(os.path.join(base, f"{sid}.frames"), index, size)
    episode_seal.seal(path)
    return episode_seal.manifest_path(path)


def _write_frames(d, index, size=(64, 48)):
    if Image is None:
        pytest.skip("needs Pillow")
    os.makedirs(d)
    with open(os.path.join(d, "index.jsonl"), "w") as idx:
        for e in index:
            f = f"{e['seq']:06d}.jpg"
            # A distinct colour per frame, so a held frame is a different picture.
            Image.new("RGB", size, ((e["seq"] * 37) % 256, (e["seq"] * 91) % 256, 40)).save(
                os.path.join(d, f), "JPEG")
            idx.write(json.dumps({"seq": e["seq"], "t": e["t"], "t_recv": e.get("t_recv", e["t"]), "bytes": 0,
                                  "w": size[0], "h": size[1], "file": f}) + "\n")


def _sealed(base, sid="20260916-101010-groot", **kw):
    rows, index = _rows(**kw)
    rows.append(_success_at(rows[-1]["t"] + 0.1))
    return _write_session(base, sid, rows, index), rows, index


def _parquet(out, i=0):
    return pq.read_table(os.path.join(out, "data", "chunk-000", f"episode_{i:06d}.parquet"))


# ---- layout ---------------------------------------------------------------------

def test_the_vector_layout_is_29_and_names_every_joint_once():
    assert ge.VECTOR_DIM == 29
    assert len(set(ge.VECTOR_NAMES)) == 29
    sl = ge.modality_slices()
    assert sl["waist"] == {"start": 0, "end": 1}
    assert sl["left_arm"] == {"start": 1, "end": 8}
    assert sl["right_arm"] == {"start": 8, "end": 15}
    assert sl["left_hand"] == {"start": 15, "end": 22}
    assert sl["right_hand"] == {"start": 22, "end": 29}
    assert ge.VECTOR_NAMES[1:15] == ARM and ge.VECTOR_NAMES[15:] == HAND


def test_the_waist_motor_agrees_with_the_onboard():
    # Read, not imported: g1_onboard needs the Unitree SDK.
    src = open(os.path.join(ROOT, "onboard", "g1_onboard.py")).read()
    assert re.search(r"^WAIST_MOTOR = 12\b", src, re.M), "onboard WAIST_MOTOR moved"
    assert ge.WAIST_MOTOR == 12


def test_the_joint_orders_match_the_retargeter():
    # Copied into the exporter so a laptop needs no URDF; pinned here.
    try:
        import retarget_g1
    except FileNotFoundError:
        pytest.skip("needs the Unitree assets (sh fetch-assets.sh)")
    assert ge.ARM_LEFT == retarget_g1.ARM_LEFT and ge.ARM_RIGHT == retarget_g1.ARM_RIGHT
    assert ge.DEX3_LEFT == retarget_g1.DEX3_LEFT and ge.DEX3_RIGHT == retarget_g1.DEX3_RIGHT


@needs_tools
def test_layout_meta_parquet_and_video_agree(tmp_path):
    mp, rows, index = _sealed(str(tmp_path))
    out = str(tmp_path / "out")
    rec = _export([mp], out, TASK, fps=15)
    for rel in ("meta/info.json", "meta/modality.json", "meta/episodes.jsonl", "meta/tasks.jsonl",
                "data/chunk-000/episode_000000.parquet",
                "videos/chunk-000/observation.images.ego_view/episode_000000.mp4",
                "meta/episodes_stats.jsonl", "new_embodiment_config.py", "wendy-export.json"):
        assert os.path.exists(os.path.join(out, rel)), rel
    assert not os.path.exists(os.path.join(out, "meta", "stats.json")), "GR00T and LeRobot v3 compute stats.json"
    info = json.load(open(os.path.join(out, "meta", "info.json")))
    assert info["codebase_version"] == "v2.1"
    assert info["fps"] == 15 and info["total_episodes"] == 1
    assert info["features"]["observation.state"]["shape"] == [29]
    assert info["features"]["action"]["names"] == ge.VECTOR_NAMES
    vid = info["features"]["observation.images.ego_view"]
    assert vid["shape"] == [48, 64, 3]
    assert vid["info"]["video.codec"] == "h264" and vid["info"]["video.fps"] == 15
    table = _parquet(out)
    mp4 = os.path.join(out, "videos", "chunk-000", "observation.images.ego_view", "episode_000000.mp4")
    assert table.num_rows == info["total_frames"] == ge.count_video_frames(mp4)
    ep_line = json.loads(open(os.path.join(out, "meta", "episodes.jsonl")).read().splitlines()[0])
    assert ep_line == {"episode_index": 0, "tasks": [TASK], "length": table.num_rows}
    assert json.loads(open(os.path.join(out, "meta", "tasks.jsonl")).read()) == {"task_index": 0, "task": TASK}
    mod = json.load(open(os.path.join(out, "meta", "modality.json")))
    ends = sorted((v["start"], v["end"]) for v in mod["state"].values())
    assert ends[0][0] == 0 and ends[-1][1] == 29
    assert all(a1 == b0 for (_, a1), (b0, _) in zip(ends, ends[1:]))
    assert mod["action"] == mod["state"]
    assert mod["video"] == {"ego_view": {"original_key": "observation.images.ego_view"}}
    assert mod["annotation"] == {"human.task_description": {"original_key": "task_index"}}
    cfg = open(os.path.join(out, "new_embodiment_config.py")).read()
    for g in mod["state"]:
        assert f'"{g}"' in cfg
    assert "annotation.human.task_description" in cfg and "register_modality_config" in cfg
    # wendy-export names its source by digest: the hash link back to the seal.
    assert rec["schema"] == "wendy.g1.groot-export.v2"
    assert rec["sources"][0]["identity"]["episode_sha256"] == json.load(open(mp))["episode"]["sha256"]
    assert rec["sources"][0]["source"]["repo"] == "g1-vr-teleop"
    assert rec["episodes"][0]["action_source"] == "sent"
    assert rec["episodes"][0]["outcome"]["result"] == "success"


@needs_tools
def test_parquet_columns_dtypes_timestamps_and_done(tmp_path):
    mp, _, _ = _sealed(str(tmp_path))
    out = str(tmp_path / "out")
    _export([mp], out, TASK, fps=15)
    t = _parquet(out)
    assert {"observation.state", "action", "timestamp", "annotation.human.task_description",
            "task_index", "episode_index", "frame_index", "index", "next.reward",
            "next.done"} <= set(t.schema.names)
    assert t.schema.field("observation.state").type == pa.list_(pa.float32())
    assert t.schema.field("timestamp").type == pa.float32()
    assert t.schema.field("episode_index").type == pa.int64()
    assert t.schema.field("next.done").type == pa.bool_()
    d = t.to_pydict()
    n = t.num_rows
    np.testing.assert_allclose(d["timestamp"], np.arange(n) / 15.0, atol=1e-6)
    assert d["frame_index"] == list(range(n)) and d["index"] == list(range(n))
    assert d["next.done"][:-1] == [False] * (n - 1) and d["next.done"][-1] is True
    assert set(d["task_index"]) == {0} == set(d["annotation.human.task_description"])
    assert all(len(v) == 29 for v in d["observation.state"])


@needs_tools
def test_action_is_zero_order_held_and_state_is_interpolated(tmp_path):
    mp, _, _ = _sealed(str(tmp_path), elbow_lag_s=0.2)
    out = str(tmp_path / "out")
    _export([mp], out, TASK, fps=15)
    d = _parquet(out).to_pydict()
    j = ge.VECTOR_NAMES.index("left_elbow_joint")
    k = 15                                        # t = 1.0 s into the ramp
    assert abs(d["action"][k][j] - 0.5) < 0.012   # last row at or before 1.0 s
    assert abs(d["observation.state"][k][j] - 0.4) < 0.01   # measured lags 0.2 s
    sl = ge.modality_slices()
    assert abs(d["observation.state"][k][sl["left_hand"]["start"]] - 0.1) < 1e-5
    assert abs(d["observation.state"][k][sl["right_hand"]["start"]] - 0.2) < 1e-5
    assert abs(d["action"][k][sl["right_hand"]["start"]] - 0.5) < 1e-5


@needs_tools
def test_sent_is_preferred_and_targets_need_a_flag(tmp_path):
    rows, index = _rows()
    for r in rows:
        for j in ARM:
            r["targets"][j] = r["sent"][j] + 0.3     # the pre-offset intent differs
    rows.append(_success_at(rows[-1]["t"] + 0.1))
    mp = _write_session(str(tmp_path), "s1", rows, index)
    rec = _export([mp], str(tmp_path / "o1"), TASK, fps=15)
    d = _parquet(str(tmp_path / "o1")).to_pydict()
    assert abs(d["action"][5][ge.VECTOR_NAMES.index("left_shoulder_pitch_joint")]) < 1e-5
    assert rec["episodes"][0]["action_source"] == "sent"
    rows2, index2 = _rows(with_sent=False)
    rows2.append(_success_at(rows2[-1]["t"] + 0.1))
    os.mkdir(tmp_path / "b")
    mp2 = _write_session(str(tmp_path / "b"), "s2", rows2, index2)
    with pytest.raises(ge.ExportRefused) as e:
        _export([mp2], str(tmp_path / "o2"), TASK, fps=15)
    assert any("no arm command" in r for r in e.value.reasons), e.value.reasons
    rec2 = _export([mp2], str(tmp_path / "o3"), TASK, fps=15, action_from_targets=True)
    assert rec2["episodes"][0]["action_source"] == "targets"
    assert any("pre-offset" in c for c in rec2["episodes"][0]["caveats"])


@needs_tools
def test_the_task_comes_from_the_header_when_not_given(tmp_path):
    mp, _, _ = _sealed(str(tmp_path))
    rec = _export([mp], str(tmp_path / "out"), None, fps=15)
    assert rec["task"] == TASK
    assert json.loads(open(tmp_path / "out" / "meta" / "tasks.jsonl").read())["task"] == TASK
    # A header without a task and no --task: refuse, say so, write nothing.
    rows, index = _rows()
    rows.append(_success_at(rows[-1]["t"] + 0.1))
    os.mkdir(tmp_path / "b")
    mp2 = _write_session(str(tmp_path / "b"), "s2", rows, index, header_extra={"task": None})
    with pytest.raises(ge.ExportRefused) as e:
        _export([mp2], str(tmp_path / "o2"), None, fps=15)
    assert any("task" in r for r in e.value.reasons), e.value.reasons
    assert not (tmp_path / "o2").exists()


# ---- refusals ------------------------------------------------------------------

@needs_tools
def test_refusals_write_nothing(tmp_path):
    mp, _, _ = _sealed(str(tmp_path))
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(ge.ExportRefused) as e:
        _export([mp], str(out), TASK)
    assert any("already exists" in r for r in e.value.reasons)
    out.rmdir()
    with pytest.raises(ge.ExportRefused) as e:
        _export([mp], str(out), "   ")
    assert any("task" in r for r in e.value.reasons)
    assert not out.exists()
    # Verify failure: one byte of the episode.
    p = tmp_path / "20260916-101010-groot.jsonl.gz"
    b = bytearray(p.read_bytes())
    b[-1] ^= 0x01
    p.write_bytes(bytes(b))
    with pytest.raises(ge.ExportRefused) as e:
        _export([mp], str(out), TASK)
    assert any("sha256" in r or "bytes" in r for r in e.value.reasons), e.value.reasons
    assert not out.exists()


@needs_tools
def test_every_attempt_by_default_and_successes_only_from_offline_labels(tmp_path):
    rows, index = _rows()
    mp = _write_session(str(tmp_path), "s", rows, index)          # the recorder writes no outcome
    rec = _export([mp], str(tmp_path / "all"), TASK)
    assert len(rec["episodes"]) == 1 and rec["episodes"][0]["outcome"] is None
    assert rec["outcomes_exported"] == "all"
    with pytest.raises(ge.ExportRefused) as e:
        _export([mp], str(tmp_path / "ok"), TASK, outcomes=frozenset({"success"}))
    assert any("0 of 1 attempts with outcome" in r for r in e.value.reasons), e.value.reasons
    # A review label beside the sealed file, bound to its digest, selects it.
    sha = json.load(open(mp))["episode"]["sha256"]
    labels = {"schema": offline_labels.SCHEMA, "session": "s", "episode_sha256": sha,
              "outcomes": [_success_at(rows[-1]["t"])]}
    (tmp_path / "s.labels.json").write_text(json.dumps(labels))
    rec = _export([mp], str(tmp_path / "ok"), TASK, outcomes=frozenset({"success"}))
    assert rec["episodes"][0]["outcome"]["result"] == "success"
    assert rec["sources"][0]["labels"] == "s.labels.json"
    # Labels bound to other bytes are refused, never applied to this file.
    (tmp_path / "s.labels.json").write_text(json.dumps(labels | {"episode_sha256": "0" * 64}))
    with pytest.raises(ge.ExportRefused) as e:
        _export([mp], str(tmp_path / "bad"), TASK)
    assert any("different episode bytes" in r for r in e.value.reasons), e.value.reasons


@needs_tools
def test_missing_frames_refuse_unsealed_frames_refuse_and_the_flag_drops_video(tmp_path):
    rows, _ = _rows()
    rows.append(_success_at(rows[-1]["t"] + 0.1))
    mp = _write_session(str(tmp_path), "s", rows, index=None)
    with pytest.raises(ge.ExportRefused) as e:
        _export([mp], str(tmp_path / "out"), TASK)
    assert any("no camera frames sealed" in r for r in e.value.reasons), e.value.reasons
    # Frames that appeared after sealing are unverified: refuse, and say to reseal.
    (tmp_path / "s.frames").mkdir()
    with pytest.raises(ge.ExportRefused) as e:
        _export([mp], str(tmp_path / "out"), TASK)
    assert any("sealed without them" in r for r in e.value.reasons), e.value.reasons
    shutil.rmtree(tmp_path / "s.frames")
    rec = _export([mp], str(tmp_path / "out"), TASK, allow_missing_frames=True)
    assert rec["groot_format"]["video_key"] is None
    assert not (tmp_path / "out" / "videos").exists()
    assert "video" not in json.load(open(tmp_path / "out" / "meta" / "modality.json"))
    assert "observation.images.ego_view" not in json.load(open(tmp_path / "out" / "meta" / "info.json"))["features"]
    assert any("without video" in c for c in rec["episodes"][0]["caveats"])


@needs_tools
def test_a_frames_tar_beside_the_episode_is_extracted_and_used(tmp_path):
    """The operator pulls <sid>.frames.tar with /session/frames; the exporter
    should not make them untar it by hand -- but it only trusts the result if
    the seal already covers those frames."""
    mp, rows, index = _sealed(str(tmp_path), sid="s")
    d = tmp_path / "s.frames"
    buf = tmp_path / "s.frames.tar"
    with tarfile.open(buf, "w") as tf:
        tf.add(d, arcname="s.frames")
    shutil.rmtree(d)
    rec = _export([mp], str(tmp_path / "out"), TASK)
    assert rec["episodes"][0]["frames"] > 0 and (tmp_path / "s.frames" / "index.jsonl").exists()
    assert rec["groot_format"]["video_key"] == "ego_view"


@needs_tools
def test_too_many_held_camera_frames_refuses(tmp_path):
    rows, index = _rows(cam_gap=(1.0, 2.5))   # camera stops for 1.5 of ~3.3 s
    rows.append(_success_at(rows[-1]["t"] + 0.1))
    mp = _write_session(str(tmp_path), "s", rows, index)
    with pytest.raises(ge.ExportRefused) as e:
        _export([mp], str(tmp_path / "out"), TASK)
    assert any("held from an older frame" in r for r in e.value.reasons), e.value.reasons
    assert not (tmp_path / "out").exists()


@needs_tools
def test_short_attempt_and_missing_hands(tmp_path):
    rows, index = _rows(n=90)                  # 1 s at 90 Hz: under the 2 s floor
    rows.append(_success_at(rows[-1]["t"] + 0.1))
    mp = _write_session(str(tmp_path), "s", rows, index)
    with pytest.raises(ge.ExportRefused) as e:
        _export([mp], str(tmp_path / "out"), TASK)
    assert any("under 2 s" in r for r in e.value.reasons), e.value.reasons
    rows2, index2 = _rows(with_hands=False)
    rows2.append(_success_at(rows2[-1]["t"] + 0.1))
    os.mkdir(tmp_path / "b")
    mp2 = _write_session(str(tmp_path / "b"), "s2", rows2, index2)
    with pytest.raises(ge.ExportRefused) as e:
        _export([mp2], str(tmp_path / "out"), TASK)
    assert any("no measured left_hand" in r for r in e.value.reasons), e.value.reasons
    rec = _export([mp2], str(tmp_path / "out"), TASK, allow_missing_hands=True)
    assert rec["episodes"][0]["zero_filled_groups"] == ["left_hand", "right_hand"]


@needs_tools
def test_two_manifests_two_episodes_global_index_continues(tmp_path):
    mp1, _, _ = _sealed(str(tmp_path), sid="s1")
    os.mkdir(tmp_path / "b")
    rows, index = _rows(t_start=T0 + 1000, waist=0.3)
    rows.append(_success_at(rows[-1]["t"] + 0.1))
    mp2 = _write_session(str(tmp_path / "b"), "s2", rows, index)
    out = str(tmp_path / "out")
    rec = _export([mp1, mp2], out, TASK)
    assert [e["episode_index"] for e in rec["episodes"]] == [0, 1]
    d0, d1 = _parquet(out, 0).to_pydict(), _parquet(out, 1).to_pydict()
    assert d1["index"][0] == d0["index"][-1] + 1 and set(d1["episode_index"]) == {1}
    assert abs(d1["observation.state"][0][0] - 0.3) < 1e-5   # waist from body idx 12
    assert abs(d1["action"][0][0] - 0.3) < 1e-5
    info = json.load(open(os.path.join(out, "meta", "info.json")))
    assert info["total_episodes"] == 2 and info["splits"] == {"train": "0:2"}
    assert len(open(os.path.join(out, "meta", "episodes.jsonl")).read().splitlines()) == 2


# ---- the CLI ---------------------------------------------------------------------

@needs_tools
def test_cli_exit_codes_and_messages(tmp_path):
    rows, index = _rows()
    rows.append(_success_at(rows[-1]["t"] + 0.1))
    mp = _write_session(str(tmp_path), "s", rows, index, header_extra={"task": None})
    out = subprocess.run([PY, CLI, mp, "--output", str(tmp_path / "out")], capture_output=True, text=True)
    assert out.returncode == 1 and "task" in out.stdout and not (tmp_path / "out").exists()
    out = subprocess.run([PY, CLI, mp, "--output", str(tmp_path / "out"), "--task", TASK],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "1 episode(s)" in out.stdout and "with video" in out.stdout
    assert (tmp_path / "out" / "wendy-export.json").exists()
    out = subprocess.run([PY, CLI, mp, "--output", str(tmp_path / "out"), "--task", TASK],
                         capture_output=True, text=True)
    assert out.returncode == 1 and "already exists" in out.stdout




# ---- LeRobot ----------------------------------------------------------------------

@needs_tools
def test_episode_stats_have_lerobots_shapes(tmp_path):
    """meta/episodes_stats.jsonl in the shapes LeRobot's compute_episode_stats
    writes: a vector keeps its length, a scalar is [1], a video is per channel
    [3,1,1] in [0,1], and count is the episode length for every feature."""
    mp, _, _ = _sealed(str(tmp_path))
    out = tmp_path / "out"
    _export([mp], str(out), TASK, fps=15)
    lines = (out / "meta" / "episodes_stats.jsonl").read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["episode_index"] == 0
    st = row["stats"]
    info = json.load(open(out / "meta" / "info.json"))
    assert set(st) == set(info["features"]), "every feature has statistics"
    n = info["total_frames"]
    keys = {"min", "max", "mean", "std", "count", "q01", "q10", "q50", "q90", "q99"}
    for name, s in st.items():
        assert set(s) == keys, name
        assert s["count"] == [n], name
    assert np.asarray(st["observation.state"]["mean"]).shape == (29,)
    assert np.asarray(st["timestamp"]["max"]).shape == (1,)
    img = st["observation.images.ego_view"]
    assert np.asarray(img["mean"]).shape == (3, 1, 1)
    assert 0.0 <= np.min(img["min"]) and np.max(img["max"]) <= 1.0
    j = ge.VECTOR_NAMES.index("left_elbow_joint")
    d = _parquet(str(out)).to_pydict()
    assert abs(st["action"]["max"][j] - max(r[j] for r in d["action"])) < 1e-6


def _lerobot_python():
    explicit = os.environ.get("LEROBOT_PYTHON")
    if explicit:
        return explicit
    probe = subprocess.run([PY, "-c", "import lerobot.scripts.convert_dataset_v21_to_v30"],
                           capture_output=True)
    return PY if probe.returncode == 0 else None


@needs_tools
def test_lerobot_converts_and_loads_both_formats(tmp_path):
    """The real check: LeRobot's own converter takes the v2.1 export to v3.0,
    and LeRobotDataset loads it with the right shapes and decodable video.
    Set LEROBOT_PYTHON to a python with lerobot[dataset] installed."""
    lr = _lerobot_python()
    if lr is None:
        pytest.skip("needs lerobot[dataset] (or LEROBOT_PYTHON)")
    mp1, _, _ = _sealed(str(tmp_path), sid="s1")
    os.mkdir(tmp_path / "b")
    rows, index = _rows(t_start=T0 + 1000, waist=0.3)
    mp2 = _write_session(str(tmp_path / "b"), "s2", rows, index)
    out = tmp_path / "ds"
    run = subprocess.run([PY, CLI, mp1, mp2, "--output", str(out), "--task", TASK, "--format", "both",
                          "--lerobot-python", lr], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "GR00T LeRobot v2.1" in run.stdout and "LeRobot v3.0" in run.stdout
    v21 = json.load(open(out / "groot" / "meta" / "info.json"))
    assert v21["codebase_version"] == "v2.1" and (out / "groot" / "meta" / "modality.json").exists()
    v3 = json.load(open(out / "lerobot-v3" / "meta" / "info.json"))
    assert v3["codebase_version"] == "v3.0" and v3["total_episodes"] == 2
    assert (out / "lerobot-v3" / "wendy-export.json").exists()
    assert not (tmp_path / "ds" / "lerobot-v3_old").exists()
    # Not an f-string: `wendy init` renders .py files as Go templates, so no
    # double opening brace may appear in this file.
    probe = """
import json
import random
from lerobot.datasets.lerobot_dataset import LeRobotDataset
ds = LeRobotDataset("local/ds", root=ROOT_PATH)
# Every frame, in a shuffled order: training samples at random, and random
# access into a later episode of a concatenated v3 video is what broke once.
order = list(range(len(ds)))
random.Random(0).shuffle(order)
for i in order:
    ds[i]
item = ds[len(ds) - 1]
print(json.dumps(dict(len=len(ds), episodes=ds.num_episodes, fps=ds.fps,
    state=list(item["observation.state"].shape), action=list(item["action"].shape),
    image=list(item["observation.images.ego_view"].shape), task=item["task"],
    stats=sorted(ds.meta.stats))))
""".replace("ROOT_PATH", repr(str(out / "lerobot-v3")))
    got = subprocess.run([lr, "-c", probe], capture_output=True, text=True)
    assert got.returncode == 0, got.stderr[-2000:]
    loaded = json.loads(got.stdout.strip().splitlines()[-1])
    assert loaded["episodes"] == 2 and loaded["len"] == v3["total_frames"] and loaded["fps"] == 15
    assert loaded["state"] == [29] and loaded["action"] == [29]
    assert loaded["image"] == [3, 48, 64]
    assert loaded["task"] == TASK
    assert {"observation.state", "action", "observation.images.ego_view"} <= set(loaded["stats"])


@needs_tools
def test_lerobot_v3_without_lerobot_refuses_and_writes_nothing(tmp_path):
    mp, _, _ = _sealed(str(tmp_path))
    run = subprocess.run([PY, CLI, mp, "--output", str(tmp_path / "v3"), "--task", TASK,
                          "--format", "lerobot-v3", "--lerobot-python", PY + "-missing"],
                         capture_output=True, text=True)
    assert run.returncode == 1 and "lerobot" in run.stdout.lower()
    assert not (tmp_path / "v3").exists()


# ---- the template's own recorder, end to end ----------------------------------------

@needs_tools
def test_a_session_from_the_real_recorder_exports(tmp_path, monkeypatch):
    """Not a fixture that imitates the recorder: the template's SessionRecorder
    writes rows and camera JPEGs, stop() closes it, episode_seal seals it as
    the bridge does at Stop session, and that is what gets exported.

    The recorder stamps rows with the wall clock as they arrive, so the clock
    is the fixture's, advanced to each row's time: the recorder sees the 3.3 s
    the rows describe, as it would live."""
    import io
    import time as _time
    import session_recorder
    clock = [T0]
    monkeypatch.setattr(session_recorder.time, "time", lambda: clock[0])
    rec = session_recorder.SessionRecorder(str(tmp_path))
    started = rec.start("", meta={"task": TASK})
    assert started["ok"], started
    rows, index = _rows()
    # Frames are kept only while ARMED: open that gate with the rows' state.
    rec.note({"t": rows[0]["t"], "hands": [], "head": None}, {}, rows[0]["robot"])
    for e in index:
        buf = io.BytesIO()
        Image.new("RGB", (64, 48), ((e["seq"] * 37) % 256, 90, 40)).save(buf, "JPEG")
        clock[0] = e["t"]
        rec.note_camera({"seq": e["seq"], "t": e["t"], "w": 64, "h": 48, "fmt": "jpeg"}, buf.getvalue())
    for r in rows:
        clock[0] = r["t"]
        rec.note({"t": r["t"], "hands": [], "head": None}, r["targets"], r["robot"],
                 sent=r.get("sent"), cam=r.get("cam"))
    deadline = _time.time() + 5
    while rec.status()["queued"] and _time.time() < deadline:
        _time.sleep(0.01)
    stopped = rec.stop()
    assert stopped["ok"] and stopped["cam_frames"] == len(index), stopped
    path = rec.path(started["session"])
    episode_seal.seal(path)
    mp = episode_seal.manifest_path(path)
    assert json.load(open(mp)).get("camera"), "the seal covers the camera frames"
    out = tmp_path / "ds"
    rec_out = _export([mp], str(out), None)          # the task comes from the header
    assert rec_out["task"] == TASK and len(rec_out["episodes"]) == 1
    ep = rec_out["episodes"][0]
    assert ep["action_source"] == "sent" and ep["frames"] >= 45 and ep["held_camera_frames"] == 0
    assert ge.count_video_frames(out / "videos" / "chunk-000" / "observation.images.ego_view"
                                 / "episode_000000.mp4") == ep["frames"]
