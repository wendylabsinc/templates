"""tools/export_dataset.py: sealed sessions -> LeRobot v2.1 + GR00T files.

A synthetic sealed session (three seconds ARMED at 90 Hz, both hand cameras
at 30 fps) goes through the real exporter, ffmpeg included; the tests check
the layout LeRobot and GR00T read, that every video has exactly as many
frames as the parquet, and that a broken input refuses with nothing written.
"""
import json
import pathlib
import shutil
import sys

import pytest

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))

np = pytest.importorskip("numpy")
pytest.importorskip("pyarrow")
Image = pytest.importorskip("PIL.Image")
if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    pytest.skip("ffmpeg/ffprobe not installed", allow_module_level=True)

import episode_seal  # noqa: E402
import export_dataset as ex  # noqa: E402

ROW_HZ, CAM_HZ, SECONDS = 90, 30, 3.0
T0_WALL, T0_MONO = 1_800_000_000.0, 5_000.0


def _jpeg(path, shade):
    Image.new("RGB", (64, 48), (shade % 256, 80, 160)).save(path, format="JPEG", quality=90)


def make_session(d, sid="20261112-120000-test", cams=("left", "right"), cam_seconds=SECONDS,
                 task="pick up the can"):
    """Write a session the way the recorder lays it out, then seal it."""
    d = pathlib.Path(d)
    d.mkdir(parents=True, exist_ok=True)
    n = int(SECONDS * ROW_HZ)
    names = ex.VECTOR_NAMES
    rows = [{"type": "header", "session": sid, "t_start": T0_WALL, "v": 1, "task": task,
             "hand_cameras": {c: {"port": f"2.3.{i}"} for i, c in enumerate(cams)}}]
    for i in range(n):
        q = [0.01 * i + 0.001 * j for j in range(14)]
        hand = {"left": [0.1] * 7, "right": [0.2] * 7}
        rows.append({
            "t": T0_WALL + i / ROW_HZ, "t_mono_ns": int((T0_MONO + i / ROW_HZ) * 1e9),
            "hands": [], "head": None,
            "robot": {"state": "ARMED", "q": q, "q_age_s": 0.0, "hand_q": hand,
                      "body": {"idx": [12], "q": [0.05]}},
            "targets": {k: 0.5 for k in names},
            "sent": {k: 0.25 for k in names},
            "hand_cams": {c: {"seq": 1, "t_cap_mono_ns": int(T0_MONO * 1e9), "age_s": 0.01}
                          for c in cams}})
    footer = {"type": "footer", "frames": n, "dropped": 0, "marks": 0,
              "outcomes": {"success": 0, "fail": 0, "abort": 0},
              "hand_cams": {c: {"frames": int(cam_seconds * CAM_HZ), "dropped": 0,
                                "skipped_not_armed": 0} for c in cams}}
    rows.append(footer)
    path = d / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    for c in cams:
        cd = d / f"{sid}.frames" / f"hand-{c}"
        cd.mkdir(parents=True)
        lines = []
        for k in range(int(cam_seconds * CAM_HZ)):
            name = f"{k + 1:06d}.jpg"
            _jpeg(cd / name, k * 3)
            lines.append(json.dumps({"seq": k + 1, "t_cap_mono_ns": int((T0_MONO + k / CAM_HZ) * 1e9),
                                     "file": name}) + "\n")
        (cd / "index.jsonl").write_text("".join(lines))
    episode_seal.seal(str(path))
    return path


def test_joint_names_match_the_retargeter():
    try:
        import retarget_g1
    except Exception as e:  # noqa: BLE001 -- needs the URDF assets (fetch-assets.sh)
        pytest.skip(f"retarget_g1 not importable here: {e}")
    assert ex.ARM_LEFT == retarget_g1.ARM_LEFT and ex.ARM_RIGHT == retarget_g1.ARM_RIGHT
    assert ex.DEX3_LEFT == retarget_g1.DEX3_LEFT and ex.DEX3_RIGHT == retarget_g1.DEX3_RIGHT
    assert ex.VECTOR_DIM == 29


def test_export_writes_lerobot_v21_and_groot_files(tmp_path):
    make_session(tmp_path / "s")
    out = tmp_path / "dataset"
    rec = ex.export(ex.find_manifests([tmp_path / "s"]), out, fps=30)
    assert rec["total_episodes"] == 1
    n = rec["total_frames"]
    assert n == int(SECONDS * 30) or n == int(SECONDS * 30) - 1 or n == int(SECONDS * 30) + 1

    info = json.loads((out / "meta" / "info.json").read_text())
    assert info["codebase_version"] == "v2.1" and info["fps"] == 30
    assert info["total_videos"] == 2
    for key in ("left_wrist_view", "right_wrist_view"):
        feat = info["features"][f"observation.images.{key}"]
        assert feat["dtype"] == "video" and feat["shape"] == [48, 64, 3]
        mp4 = out / "videos" / "chunk-000" / f"observation.images.{key}" / "episode_000000.mp4"
        assert ex.count_video_frames(mp4) == n

    import pyarrow.parquet as pq
    t = pq.read_table(out / "data" / "chunk-000" / "episode_000000.parquet").to_pydict()
    assert len(t["frame_index"]) == n
    assert len(t["observation.state"][0]) == 29 and len(t["action"][0]) == 29
    # State is measured: waist from body motor 12, arms from robot.q, hands from hand_q.
    s0 = t["observation.state"][0]
    assert s0[0] == pytest.approx(0.05)
    assert s0[1:15] == pytest.approx([0.001 * j for j in range(14)], abs=1e-5)
    assert s0[15:22] == pytest.approx([0.1] * 7) and s0[22:29] == pytest.approx([0.2] * 7)
    # Action is what was sent, not the targets.
    assert t["action"][0] == pytest.approx([0.25] * 29)

    modality = json.loads((out / "meta" / "modality.json").read_text())
    assert modality["state"]["left_arm"] == {"start": 1, "end": 8}
    assert modality["state"]["right_hand"] == {"start": 22, "end": 29}
    assert set(modality["video"]) == {"left_wrist_view", "right_wrist_view"}
    assert modality["annotation"]["human.task_description"] == {"original_key": "task_index"}

    tasks = [json.loads(l) for l in (out / "meta" / "tasks.jsonl").read_text().splitlines()]
    assert tasks == [{"task_index": 0, "task": "pick up the can"}]
    eps = [json.loads(l) for l in (out / "meta" / "episodes.jsonl").read_text().splitlines()]
    assert eps == [{"episode_index": 0, "tasks": ["pick up the can"], "length": n}]
    stats = [json.loads(l) for l in (out / "meta" / "episodes_stats.jsonl").read_text().splitlines()]
    assert len(stats[0]["stats"]["observation.state"]["mean"]) == 29
    assert len(stats[0]["stats"]["observation.images.left_wrist_view"]["mean"]) == 3

    cfg = (out / "new_embodiment_config.py").read_text()
    assert "left_wrist_view" in cfg and "right_wrist_view" in cfg
    record = json.loads((out / "wendy-export.json").read_text())
    assert record["episodes"][0]["held_frames"] == {"left_wrist_view": 0, "right_wrist_view": 0}
    assert record["sessions"][0]["episode_sha256"]


def test_a_tampered_frame_refuses_and_writes_nothing(tmp_path):
    p = make_session(tmp_path / "s")
    (p.parent / f"{p.stem}.frames" / "hand-left" / "000010.jpg").write_bytes(b"\xff\xd8edited")
    with pytest.raises(ex.ExportRefused) as e:
        ex.export(ex.find_manifests([tmp_path / "s"]), tmp_path / "dataset")
    assert any("left hand camera frames sha256 differs" in r for r in e.value.reasons)
    assert not (tmp_path / "dataset").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".dataset-")]


def test_a_camera_that_stopped_halfway_is_held_and_refused(tmp_path):
    make_session(tmp_path / "s", cam_seconds=SECONDS / 2)
    with pytest.raises(ex.ExportRefused) as e:
        ex.export(ex.find_manifests([tmp_path / "s"]), tmp_path / "dataset")
    assert any("camera held" in r for r in e.value.reasons)


def test_a_missing_camera_is_named(tmp_path):
    make_session(tmp_path / "s", cams=("left",))
    with pytest.raises(ex.ExportRefused) as e:
        ex.export(ex.find_manifests([tmp_path / "s"]), tmp_path / "dataset")
    assert any("no right camera frames" in r for r in e.value.reasons)
    rec = ex.export(ex.find_manifests([tmp_path / "s"]), tmp_path / "dataset", cameras=("left",))
    assert rec["cameras"] == {"left": "left_wrist_view"}


def test_an_existing_output_is_not_replaced_without_overwrite(tmp_path):
    make_session(tmp_path / "s")
    (tmp_path / "dataset").mkdir()
    with pytest.raises(ex.ExportRefused):
        ex.export(ex.find_manifests([tmp_path / "s"]), tmp_path / "dataset")
    rec = ex.export(ex.find_manifests([tmp_path / "s"]), tmp_path / "dataset", overwrite=True)
    assert rec["total_episodes"] == 1


def test_the_cli_reports_refusals(tmp_path, capsys):
    assert ex.main([str(tmp_path / "nothing-here"), "--out", str(tmp_path / "d")]) == 1
    assert "nothing was written" in capsys.readouterr().err
