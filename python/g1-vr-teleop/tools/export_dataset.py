#!/usr/bin/env python3
"""Export sealed sessions as a LeRobot v2.1 dataset that GR00T can train on.

    python tools/export_dataset.py --out dataset --task "pick up the can" sessions/

Run it on a laptop, after copying sessions off the robot (see README, "Export
to LeRobot and GR00T"). It needs numpy, pyarrow, Pillow
(tools/requirements-export.txt) and the system ffmpeg/ffprobe.

What comes out:

    meta/info.json             LeRobot v2.1: fps, features, paths
    meta/episodes.jsonl        one line per episode
    meta/tasks.jsonl           the language instructions
    meta/episodes_stats.jsonl  per-episode feature statistics (LeRobot v2.1)
    meta/modality.json         GR00T: the slices of state/action, video keys
    data/chunk-000/episode_000000.parquet
    videos/chunk-000/observation.images.<key>/episode_000000.mp4
    new_embodiment_config.py   GR00T N1.7 modality config for these keys
    wendy-export.json          which sealed sessions went in, by digest

One episode per attempt (episode_check.attempts: split at marks, pauses and
holds). Only ARMED rows are demonstration. State is what the robot measured,
action what it was told (`sent`), both in one 29-vector:

    waist[1] left_arm[7] right_arm[7] left_hand[7] right_hand[7]

Video keys: left_wrist_view and right_wrist_view from the hand cameras,
ego_view from the head camera, each only when the session has it. Every
camera is resampled onto the same grid as the joints, by capture time on the
robot's monotonic clock, so frame k of every video and row k of the parquet
are the same instant.

Rules, from the conference exporter this is ported from: every manifest is
verified before anything is read; a measurement is never invented (a hole
refuses the attempt, with every reason listed); a camera that holds more than
MAX_HELD_FRAC of its frames refuses the attempt; and nothing is written until
every attempt has passed, so a refusal leaves no half-dataset behind.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "teleop" / "bridge"))
import episode_check  # noqa: E402
import episode_seal  # noqa: E402

EXPORT_SCHEMA = "wendy.g1.lerobot-export.v1"
CODEBASE_VERSION = "v2.1"
ROBOT_TYPE = "unitree_g1_29_upper_body_dex3"
DEFAULT_FPS = 30
CHUNK_SIZE = 1000
MIN_ATTEMPT_S = 2.0      # shorter than this is a twitch, not a demonstration
FRESH_S = 0.1            # a measurement older than this is not this grid point's
CAM_FRESH_S = 0.1        # a frame older than this (or 1.5x the camera's interval) is held
CAM_INTERVAL_FACTOR = 1.5
MAX_HELD_FRAC = 0.2      # more held frames than this and the video lies about motion
STATS_IMAGES = 40        # frames sampled per video for episodes_stats
ANNOTATION_KEY = "human.task_description"
ANNOTATION_COLUMN = "annotation." + ANNOTATION_KEY
WAIST_MOTOR = 12         # Unitree motor index of waist yaw (onboard WAIST_MOTOR)

# Copies of teleop/bridge/retarget_g1.py ARM_* / DEX3_*: that module parses
# the URDF when imported, which a laptop export should not need. A test pins
# them equal.
ARM_LEFT = ["left_shoulder_pitch_joint", "left_shoulder_roll_joint", "left_shoulder_yaw_joint",
            "left_elbow_joint", "left_wrist_roll_joint", "left_wrist_pitch_joint",
            "left_wrist_yaw_joint"]
ARM_RIGHT = [n.replace("left", "right") for n in ARM_LEFT]
DEX3_LEFT = ["left_hand_thumb_0_joint", "left_hand_thumb_1_joint", "left_hand_thumb_2_joint",
             "left_hand_middle_0_joint", "left_hand_middle_1_joint",
             "left_hand_index_0_joint", "left_hand_index_1_joint"]
DEX3_RIGHT = [n.replace("left", "right") for n in DEX3_LEFT]

GROUPS = [("waist", ["waist_yaw_joint"]), ("left_arm", ARM_LEFT), ("right_arm", ARM_RIGHT),
          ("left_hand", DEX3_LEFT), ("right_hand", DEX3_RIGHT)]
VECTOR_NAMES = [n for _, names in GROUPS for n in names]
VECTOR_DIM = len(VECTOR_NAMES)   # 29

# Recording camera name -> dataset video key.
VIDEO_KEYS = {"left": "left_wrist_view", "right": "right_wrist_view", "head": "ego_view"}


class ExportRefused(ValueError):
    """Nothing is written. `reasons` lists every reason, not just the first."""

    def __init__(self, reasons):
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


def modality_slices():
    out, start = {}, 0
    for key, names in GROUPS:
        out[key] = {"start": start, "end": start + len(names)}
        start += len(names)
    return out


# ---- finding and loading sessions ------------------------------------------------

def find_manifests(paths):
    """Directories -> every *.manifest.json in them; files as given."""
    out = []
    for p in map(Path, paths):
        if p.is_dir():
            out.extend(sorted(p.glob("*.manifest.json")))
        else:
            out.append(p)
    return out


def _extract_frames_tar(base, sid):
    """<sid>.frames.tar (what /session/frames returns) -> <sid>.frames/ beside it."""
    tar = os.path.join(base, sid + ".frames.tar")
    if os.path.isdir(os.path.join(base, sid + ".frames")) or not os.path.isfile(tar):
        return
    with tarfile.open(tar) as tf:
        tf.extractall(base, filter="data")


def load(manifest_path):
    """Verify the seal, then read. -> (manifest, episode). Raises ExportRefused."""
    manifest_path = os.path.abspath(str(manifest_path))
    base = os.path.dirname(manifest_path)
    try:
        with open(manifest_path) as fh:
            m = json.load(fh)
    except (OSError, ValueError) as e:
        raise ExportRefused([f"{os.path.basename(manifest_path)}: manifest unreadable: {e}"])
    sid = m.get("session") or os.path.basename(manifest_path)[:-len(".manifest.json")]
    _extract_frames_tar(base, sid)
    rep = episode_seal.verify(manifest_path)
    if not rep["ok"]:
        raise ExportRefused([f"{sid}: {r}" for r in rep["reasons"]])
    ep_path = os.path.join(base, (m.get("episode") or {}).get("file") or "")
    header, body, footer, marks, outcomes = episode_check.load(ep_path)
    return m, {"sid": sid, "base": base, "header": header or {}, "footer": footer or {},
               "frames": body, "marks": marks, "outcomes": outcomes, "path": ep_path}


# ---- one clock --------------------------------------------------------------------

def clock_offset(rows):
    """Seconds to add to a row's wall `t` to get CLOCK_MONOTONIC, or None.

    Rows recorded with hand cameras carry t_mono_ns; the head camera's index
    and older sessions only have wall time. The median difference over the
    session maps one onto the other well inside a frame interval."""
    d = [r["t_mono_ns"] / 1e9 - float(r["t"]) for r in rows
         if isinstance(r.get("t_mono_ns"), int) and isinstance(r.get("t"), (int, float))]
    return float(np.median(d)) if d else None


def row_time(row, offset):
    if offset is not None and isinstance(row.get("t_mono_ns"), int):
        return row["t_mono_ns"] / 1e9
    return float(row["t"]) + (offset or 0.0)


# ---- resampling (as the conference exporter) ---------------------------------------

def _zoh(sample_t, sample_v, grid_t):
    """Last sample at or before each grid point; NaN before the first."""
    out = np.full((len(grid_t), sample_v.shape[1]), np.nan)
    if len(sample_t) == 0:
        return out
    idx = np.searchsorted(sample_t, grid_t, side="right") - 1
    ok = idx >= 0
    out[ok] = sample_v[idx[ok]]
    return out


def _linear_fresh(sample_t, sample_v, grid_t, fresh_s=FRESH_S):
    """Linear between bracketing samples, only where one is within fresh_s."""
    out = np.full((len(grid_t), sample_v.shape[1]), np.nan)
    if len(sample_t) == 0:
        return out
    pos = np.searchsorted(sample_t, grid_t)
    lo = np.clip(pos - 1, 0, len(sample_t) - 1)
    hi = np.clip(pos, 0, len(sample_t) - 1)
    d_lo, d_hi = np.abs(grid_t - sample_t[lo]), np.abs(sample_t[hi] - grid_t)
    nearest_ok = np.minimum(d_lo, d_hi) <= fresh_s
    both = (sample_t[lo] <= grid_t) & (grid_t <= sample_t[hi]) & (hi != lo) & nearest_ok
    if both.any():
        span = sample_t[hi[both]] - sample_t[lo[both]]
        w = (grid_t[both] - sample_t[lo[both]]) / np.where(span > 0, span, 1.0)
        out[both] = sample_v[lo[both]] + (sample_v[hi[both]] - sample_v[lo[both]]) * w[:, None]
    single = nearest_ok & ~both
    if single.any():
        pick = np.where(d_lo[single] <= d_hi[single], lo[single], hi[single])
        out[single] = sample_v[pick]
    return out


def _samples(rows, pick, t_of):
    ts, vs, seen = [], [], set()
    for r in rows:
        v = pick(r)
        if v is None:
            continue
        t = t_of(r)
        if t in seen:
            continue
        seen.add(t)
        ts.append(t)
        vs.append(v)
    if not ts:
        return np.zeros(0), np.zeros((0, 1))
    order = np.argsort(ts)
    return np.asarray(ts, float)[order], np.asarray(vs, float)[order]


def _waist_q(row):
    rb = row.get("robot") or {}
    b = rb.get("body")
    if isinstance(b, dict) and isinstance(b.get("idx"), list) and isinstance(b.get("q"), list):
        for i, v in zip(b["idx"], b["q"]):
            if int(i) == WAIST_MOTOR and isinstance(v, (int, float)):
                return [float(v)]
    w = rb.get("waist")
    if isinstance(w, dict) and isinstance(w.get("q"), (int, float)):
        return [float(w["q"])]
    return None


def _arm_q(row):
    q = (row.get("robot") or {}).get("q")
    return [float(v) for v in q] if isinstance(q, list) and len(q) == 14 else None


def _hand_q(row, side):
    h = (row.get("robot") or {}).get("hand_q")
    v = h.get(side) if isinstance(h, dict) else None
    return [float(x) for x in v] if isinstance(v, list) and len(v) == 7 else None


def _commanded(row, names, allow_targets):
    s = row.get("sent")
    if isinstance(s, dict) and all(isinstance(s.get(n), (int, float)) for n in names):
        return [float(s[n]) for n in names], "sent"
    if allow_targets:
        t = row.get("targets") or {}
        if all(isinstance(t.get(n), (int, float)) for n in names):
            return [float(t[n]) for n in names], "targets"
    return None


def _is_armed(row):
    return str((row.get("robot") or {}).get("state") or "").startswith("ARMED")


def build_episode(attempt, rows, fps, offset, *, action_from_targets=False,
                  allow_missing_hands=False):
    """One attempt's ARMED rows on a fixed grid. Raises ExportRefused."""
    label = f"attempt {attempt['index']} ({attempt['label']})"
    rows = [r for r in rows if _is_armed(r)]
    if len(rows) < 2:
        raise ExportRefused([f"{label}: fewer than two ARMED rows"])
    t_rows = np.asarray([row_time(r, offset) for r in rows])
    reasons, caveats, zero_filled = [], [], []
    if t_rows[-1] - t_rows[0] < MIN_ATTEMPT_S:
        reasons.append(f"{label}: only {t_rows[-1] - t_rows[0]:.2f} s ARMED, under {MIN_ATTEMPT_S:.0f} s")
    grid = t_rows[0] + np.arange(int(np.floor((t_rows[-1] - t_rows[0]) * fps)) + 1) / float(fps)
    sl = modality_slices()

    def t_meas(r):
        age = (r.get("robot") or {}).get("q_age_s")
        return row_time(r, offset) - (float(age) if isinstance(age, (int, float)) else 0.0)

    state = np.full((len(grid), VECTOR_DIM), np.nan)

    def fill(group, pick):
        s_t, s_v = _samples(rows, pick, t_meas)
        if len(s_t):
            state[:, sl[group]["start"]:sl[group]["end"]] = _linear_fresh(s_t, s_v, grid)

    fill("waist", _waist_q)
    fill("left_arm", lambda r: _arm_q(r)[:7] if _arm_q(r) else None)
    fill("right_arm", lambda r: _arm_q(r)[7:] if _arm_q(r) else None)
    fill("left_hand", lambda r: _hand_q(r, "left"))
    fill("right_hand", lambda r: _hand_q(r, "right"))
    for group in ("left_hand", "right_hand"):
        cols = state[:, sl[group]["start"]:sl[group]["end"]]
        if np.isnan(cols).all():
            if allow_missing_hands:
                cols[:] = 0.0
                zero_filled.append(group)
            else:
                reasons.append(f"{label}: no measured {group} (robot.hand_q); "
                               "--allow-missing-hands zero-fills it and records that")
    bad = [VECTOR_NAMES[j] for j in range(VECTOR_DIM) if np.isnan(state[:, j]).any()]
    if bad:
        holes = int(np.isnan(state).any(axis=1).sum())
        reasons.append(f"{label}: no fresh measurement within {FRESH_S:.2f} s at {holes} of "
                       f"{len(grid)} grid points for {', '.join(bad[:6])}"
                       + (" ..." if len(bad) > 6 else ""))

    action = np.full((len(grid), VECTOR_DIM), np.nan)
    arm_src = set()
    for group, names, allow in (("left_arm", ARM_LEFT, action_from_targets),
                                ("right_arm", ARM_RIGHT, action_from_targets),
                                # No arming offset on the waist or hands: their
                                # targets ARE what was sent.
                                ("waist", ["waist_yaw_joint"], True),
                                ("left_hand", DEX3_LEFT, True),
                                ("right_hand", DEX3_RIGHT, True)):
        ts, vs = [], []
        for r in rows:
            got = _commanded(r, names, allow)
            if got is None:
                continue
            ts.append(row_time(r, offset))
            vs.append(got[0])
            if group.endswith("_arm"):
                arm_src.add(got[1])
        if ts:
            action[:, sl[group]["start"]:sl[group]["end"]] = _zoh(np.asarray(ts), np.asarray(vs), grid)
    if not arm_src:
        reasons.append(f"{label}: rows carry no arm command (`sent`); --action-from-targets "
                       "uses the pre-offset targets and records that")
    elif arm_src == {"targets"}:
        caveats.append("arm action from pre-offset `targets`, not the command sent")
    bad_a = [VECTOR_NAMES[j] for j in range(VECTOR_DIM) if np.isnan(action[:, j]).any()]
    if bad_a and arm_src:
        reasons.append(f"{label}: no command for {', '.join(bad_a[:6])}"
                       + (" ..." if len(bad_a) > 6 else ""))
    if reasons:
        raise ExportRefused(reasons)
    return {"attempt": attempt["index"], "label": attempt["label"],
            "outcome": attempt.get("outcome"), "grid": grid, "rows": rows,
            "armed_seconds": round(float(t_rows[-1] - t_rows[0]), 3),
            "state": state.astype(np.float32), "action": action.astype(np.float32),
            "zero_filled": zero_filled, "caveats": caveats}


# ---- cameras ----------------------------------------------------------------------

def _read_index(path):
    try:
        with open(path) as fh:
            return [json.loads(l) for l in fh if l.strip()]
    except (OSError, ValueError):
        return []


def camera_frames(ep):
    """-> {camera name: [(capture time on the grid's clock, file path)]}.

    Hand cameras: <sid>.frames/hand-<name>/index.jsonl, capture time on
    CLOCK_MONOTONIC. Head camera: <sid>.frames/index.jsonl, the bridge's
    receive time (wall clock), mapped with the session's clock offset."""
    frames_dir = os.path.join(ep["base"], ep["sid"] + ".frames")
    out = {}
    if not os.path.isdir(frames_dir):
        return out
    for sub in sorted(os.listdir(frames_dir)):
        d = os.path.join(frames_dir, sub)
        if not (sub.startswith("hand-") and os.path.isdir(d)):
            continue
        items = [(r["t_cap_mono_ns"] / 1e9, os.path.join(d, r["file"]))
                 for r in _read_index(os.path.join(d, "index.jsonl"))
                 if isinstance(r.get("t_cap_mono_ns"), int) and r.get("file")]
        if items:
            out[sub[len("hand-"):]] = sorted(items)
    head = []
    for r in _read_index(os.path.join(frames_dir, "index.jsonl")):
        t = r.get("t_recv", r.get("t"))
        if isinstance(t, (int, float)) and r.get("file"):
            head.append((float(t) + (ep["offset"] or 0.0), os.path.join(frames_dir, r["file"])))
    if head:
        out["head"] = sorted(head)
    return out


def fresh_window(times):
    """A frame is held when a newer one should have come and did not: the
    larger of CAM_FRESH_S and 1.5x the camera's median interval."""
    gaps = np.diff(np.asarray(times))
    gaps = gaps[gaps > 0]
    return float(max(CAM_FRESH_S, CAM_INTERVAL_FACTOR * float(np.median(gaps)))) if len(gaps) else CAM_FRESH_S


def choose_frames(grid, items):
    """For each grid point the newest frame captured at or before it.
    -> (files, held). Before the first capture the first frame is used, held."""
    times = np.asarray([t for t, _ in items])
    window = fresh_window(times)
    idx = np.searchsorted(times, grid, side="right") - 1
    files, held = [], 0
    for k, i in enumerate(idx):
        if i < 0:
            files.append(items[0][1])
            held += 1
            continue
        files.append(items[i][1])
        if grid[k] - times[i] > window:
            held += 1
    return files, held


def encode_video(files, fps, out_mp4, ffmpeg="ffmpeg"):
    """JPEGs at fps -> H.264 yuv420p mp4. -> (height, width)."""
    from PIL import Image
    with Image.open(files[0]) as im:
        w, h = im.size
    if w % 2 or h % 2:
        raise ExportRefused([f"{os.path.basename(out_mp4)}: frames are {w}x{h}; yuv420p needs even sizes"])
    Path(out_mp4).parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="export-frames-") as td:
        for i, f in enumerate(files):
            os.symlink(os.path.abspath(f), os.path.join(td, f"{i:06d}.jpg"))
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-framerate", str(fps), "-i", os.path.join(td, "%06d.jpg"),
                        "-frames:v", str(len(files)), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-g", str(fps), "-crf", "18", "-movflags", "+faststart", str(out_mp4)],
                       check=True)
    return h, w


def count_video_frames(path, ffprobe="ffprobe"):
    out = subprocess.run([ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
                          "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return int(out.split(",")[0])


def tools_available():
    missing = []
    for mod, name in (("pyarrow", "pyarrow"), ("PIL", "Pillow")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(name)
    missing += [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    return missing


# ---- statistics (meta/episodes_stats.jsonl) ----------------------------------------

def _num_stats(a):
    a = np.asarray(a, dtype=np.float64)
    if a.ndim == 1:
        a = a[:, None]
    return {"min": a.min(0).tolist(), "max": a.max(0).tolist(), "mean": a.mean(0).tolist(),
            "std": a.std(0).tolist(), "count": [int(a.shape[0])]}


def _image_stats(files):
    """Per-channel stats over sampled frames, values in [0, 1], shaped
    [3][1][1] as LeRobot writes them for images."""
    from PIL import Image
    pick = np.unique(np.linspace(0, len(files) - 1, min(STATS_IMAGES, len(files))).astype(int))
    px = []
    for i in pick:
        with Image.open(files[i]) as im:
            a = np.asarray(im.convert("RGB").resize((80, 60)), dtype=np.float64) / 255.0
        px.append(a.reshape(-1, 3))
    a = np.concatenate(px)
    shape = lambda v: [[[float(x)]] for x in v]  # noqa: E731
    return {"min": shape(a.min(0)), "max": shape(a.max(0)), "mean": shape(a.mean(0)),
            "std": shape(a.std(0)), "count": [int(len(pick))]}


# ---- writing --------------------------------------------------------------------

def write_parquet(path, ep, episode_index, task_index, global_start, fps):
    import pyarrow as pa
    import pyarrow.parquet as pq
    n = len(ep["grid"])
    # Snapped to the grid: LeRobot checks timestamp * fps against frame_index.
    ts = (np.arange(n) / float(fps)).astype(np.float32)
    table = pa.table({
        "observation.state": pa.array(list(ep["state"]), type=pa.list_(pa.float32())),
        "action": pa.array(list(ep["action"]), type=pa.list_(pa.float32())),
        "timestamp": pa.array(ts, type=pa.float32()),
        ANNOTATION_COLUMN: pa.array(np.full(n, task_index, dtype=np.int64)),
        "task_index": pa.array(np.full(n, task_index, dtype=np.int64)),
        "episode_index": pa.array(np.full(n, episode_index, dtype=np.int64)),
        "frame_index": pa.array(np.arange(n, dtype=np.int64)),
        "index": pa.array(np.arange(global_start, global_start + n, dtype=np.int64)),
        "next.reward": pa.array(np.zeros(n, dtype=np.float32)),
        "next.done": pa.array(np.arange(n) == n - 1),
    })
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    return {"observation.state": _num_stats(ep["state"]), "action": _num_stats(ep["action"]),
            "timestamp": _num_stats(ts), "frame_index": _num_stats(np.arange(n)),
            "episode_index": _num_stats(np.full(n, episode_index)),
            "index": _num_stats(np.arange(global_start, global_start + n)),
            "task_index": _num_stats(np.full(n, task_index))}


def info_json(fps, total_episodes, total_frames, total_tasks, videos):
    """videos: {video key: (height, width)}."""
    scalar = lambda dtype: {"dtype": dtype, "shape": [1], "names": None}  # noqa: E731
    features = {
        "observation.state": {"dtype": "float32", "shape": [VECTOR_DIM], "names": list(VECTOR_NAMES)},
        "action": {"dtype": "float32", "shape": [VECTOR_DIM], "names": list(VECTOR_NAMES)},
    }
    for key, (h, w) in sorted(videos.items()):
        features[f"observation.images.{key}"] = {
            "dtype": "video", "shape": [h, w, 3], "names": ["height", "width", "channels"],
            "info": {"video.height": h, "video.width": w, "video.codec": "h264",
                     "video.pix_fmt": "yuv420p", "video.is_depth_map": False,
                     "video.fps": fps, "video.channels": 3, "has_audio": False}}
    for name, dtype in (("timestamp", "float32"), ("frame_index", "int64"),
                        ("episode_index", "int64"), ("index", "int64"), ("task_index", "int64"),
                        (ANNOTATION_COLUMN, "int64"), ("next.reward", "float32"),
                        ("next.done", "bool")):
        features[name] = scalar(dtype)
    return {"codebase_version": CODEBASE_VERSION, "robot_type": ROBOT_TYPE,
            "total_episodes": total_episodes, "total_frames": total_frames,
            "total_tasks": total_tasks, "total_videos": total_episodes * len(videos),
            "total_chunks": (total_episodes + CHUNK_SIZE - 1) // CHUNK_SIZE,
            "chunks_size": CHUNK_SIZE, "fps": fps,
            "splits": {"train": f"0:{total_episodes}"},
            "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
            "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
            "features": features}


def modality_json(video_keys):
    sl = modality_slices()
    out = {"state": sl, "action": dict(sl),
           "annotation": {ANNOTATION_KEY: {"original_key": "task_index"}}}
    if video_keys:
        out["video"] = {k: {"original_key": f"observation.images.{k}"} for k in sorted(video_keys)}
    return out


EMBODIMENT_CONFIG = '''"""GR00T N1.7 modality config for the Wendy G1 upper body + Dex3 dataset.

Register with:  --modality-config-path <dataset>/new_embodiment_config.py
State/action layout (29): waist[1] left_arm[7] right_arm[7] left_hand[7] right_hand[7],
radians, absolute in the dataset. Arms use RELATIVE actions (the processor
derives them from the absolute state); waist and hands stay ABSOLUTE.
"""
from gr00t.configs.data.embodiment_configs import MODALITY_CONFIGS, register_modality_config
from gr00t.data.embodiment_tags import EmbodimentTag
from gr00t.data.types import (
    ActionConfig,
    ActionFormat,
    ActionRepresentation,
    ActionType,
    ModalityConfig,
)


def _action(rep):
    return ActionConfig(rep=rep, type=ActionType.NON_EEF, format=ActionFormat.DEFAULT)


GROUPS = ["waist", "left_arm", "right_arm", "left_hand", "right_hand"]

config = {
__VIDEO__    "state": ModalityConfig(
        delta_indices=[0],
        modality_keys=GROUPS,
        sin_cos_embedding_keys=["left_arm", "right_arm", "waist"],
    ),
    "action": ModalityConfig(
        delta_indices=list(range(0, 16)),
        modality_keys=GROUPS,
        action_configs=[
            _action(ActionRepresentation.ABSOLUTE),   # waist
            _action(ActionRepresentation.RELATIVE),   # left_arm
            _action(ActionRepresentation.RELATIVE),   # right_arm
            _action(ActionRepresentation.ABSOLUTE),   # left_hand
            _action(ActionRepresentation.ABSOLUTE),   # right_hand
        ],
    ),
    "language": ModalityConfig(delta_indices=[0], modality_keys=["__ANNOTATION__"]),
}

MODALITY_CONFIGS.pop(EmbodimentTag.NEW_EMBODIMENT.value, None)
register_modality_config(config, embodiment_tag=EmbodimentTag.NEW_EMBODIMENT)
'''


def embodiment_config_text(video_keys):
    video = (f'    "video": ModalityConfig(delta_indices=[0], modality_keys={sorted(video_keys)!r}),\n'
             if video_keys else "    # no video in this dataset\n")
    return EMBODIMENT_CONFIG.replace("__VIDEO__", video).replace("__ANNOTATION__", ANNOTATION_COLUMN)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- the export -----------------------------------------------------------------

def plan(manifest_paths, *, fps, cameras, outcomes, action_from_targets=False,
         allow_missing_hands=False, max_held=MAX_HELD_FRAC):
    """Read, verify and resample everything; write nothing. -> (episodes,
    sessions). Raises ExportRefused with every reason found."""
    reasons, episodes, sessions = [], [], []
    for mp in manifest_paths:
        try:
            m, ep = load(mp)
        except ExportRefused as e:
            reasons += e.reasons
            continue
        ep["offset"] = clock_offset(ep["frames"])
        cams = camera_frames(ep)
        sessions.append({"session": ep["sid"], "manifest": os.path.basename(str(mp)),
                         "manifest_sha256": _sha256(mp),
                         "episode_sha256": (m.get("episode") or {}).get("sha256"),
                         "hand_cameras": ep["header"].get("hand_cameras"),
                         "cameras_found": sorted(cams)})
        atts = episode_check.attempts(ep["frames"], ep["marks"], outcomes=ep["outcomes"])
        for i, a in enumerate(atts):
            a["index"] = i
            if outcomes is not None and (a.get("outcome") or {}).get("result") not in outcomes:
                continue
            a_rows = [r for r in ep["frames"] if a["t0"] <= float(r["t"]) <= a["t1"]]
            if not any(_is_armed(r) for r in a_rows):
                continue           # an attempt with no ARMED row is not a demonstration
            try:
                e = build_episode(a, a_rows, fps, ep["offset"],
                                  action_from_targets=action_from_targets,
                                  allow_missing_hands=allow_missing_hands)
            except ExportRefused as err:
                reasons += [f"{ep['sid']}: {r}" for r in err.reasons]
                continue
            e["videos"], e["held"] = {}, {}
            for cam in cameras:
                key = VIDEO_KEYS[cam]
                if cam not in cams:
                    reasons.append(f"{ep['sid']}: attempt {i}: no {cam} camera frames "
                                   f"(drop it with --cameras)")
                    continue
                files, held = choose_frames(e["grid"], cams[cam])
                if held > max_held * len(files):
                    reasons.append(f"{ep['sid']}: attempt {i}: {cam} camera held {held} of "
                                   f"{len(files)} frames, over {100 * max_held:.0f} %")
                e["videos"][key], e["held"][key] = files, held
            e["session"], e["task"] = ep["sid"], ep["header"].get("task")
            episodes.append(e)
    if not reasons and not episodes:
        reasons.append("no ARMED attempt matched; nothing to export")
    if reasons:
        raise ExportRefused(reasons)
    return episodes, sessions


def export(manifest_paths, output, *, task=None, fps=DEFAULT_FPS, cameras=("left", "right"),
           outcomes=None, action_from_targets=False, allow_missing_hands=False,
           max_held=MAX_HELD_FRAC, overwrite=False):
    missing = tools_available()
    if missing:
        raise ExportRefused([f"missing: {', '.join(missing)} (pip install -r tools/requirements-export.txt; "
                             "ffmpeg from your package manager)"])
    output = Path(output)
    if output.exists() and not overwrite:
        raise ExportRefused([f"{output} exists; pass --overwrite to replace it"])
    episodes, sessions = plan(manifest_paths, fps=fps, cameras=cameras, outcomes=outcomes,
                              action_from_targets=action_from_targets,
                              allow_missing_hands=allow_missing_hands, max_held=max_held)
    tasks = []
    for e in episodes:
        e["task_text"] = task or e.get("task") or "teleoperated demonstration"
        if e["task_text"] not in tasks:
            tasks.append(e["task_text"])
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=str(output.parent)))
    try:
        videos_hw, total, ep_lines, stats_lines, records = {}, 0, [], [], []
        for k, e in enumerate(episodes):
            chunk = f"chunk-{k // CHUNK_SIZE:03d}"
            t_index = tasks.index(e["task_text"])
            stats = write_parquet(work / "data" / chunk / f"episode_{k:06d}.parquet",
                                  e, k, t_index, total, fps)
            n = len(e["grid"])
            for key, files in e["videos"].items():
                mp4 = work / "videos" / chunk / f"observation.images.{key}" / f"episode_{k:06d}.mp4"
                hw = encode_video(files, fps, mp4)
                if videos_hw.setdefault(key, hw) != hw:
                    raise ExportRefused([f"{key}: frame size {hw} differs from {videos_hw[key]} "
                                         "in an earlier episode"])
                got = count_video_frames(mp4)
                if got != n:
                    raise ExportRefused([f"{mp4.name} ({key}) has {got} frames, parquet has {n}"])
                stats[f"observation.images.{key}"] = _image_stats(files)
            ep_lines.append({"episode_index": k, "tasks": [e["task_text"]], "length": n})
            stats_lines.append({"episode_index": k, "stats": stats})
            records.append({"episode_index": k, "session": e["session"], "attempt": e["attempt"],
                            "label": e["label"], "outcome": e.get("outcome"),
                            "frames": n, "armed_seconds": e["armed_seconds"],
                            "held_frames": e["held"], "zero_filled": e["zero_filled"],
                            "caveats": e["caveats"]})
            total += n
        if set(videos_hw) != {VIDEO_KEYS[c] for c in cameras}:
            raise ExportRefused(["not every episode has every camera"])
        meta = work / "meta"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "info.json").write_text(json.dumps(
            info_json(fps, len(episodes), total, len(tasks), videos_hw), indent=2) + "\n")
        (meta / "modality.json").write_text(json.dumps(modality_json(videos_hw), indent=2) + "\n")
        (meta / "tasks.jsonl").write_text("".join(
            json.dumps({"task_index": i, "task": t}) + "\n" for i, t in enumerate(tasks)))
        (meta / "episodes.jsonl").write_text("".join(json.dumps(l) + "\n" for l in ep_lines))
        (meta / "episodes_stats.jsonl").write_text("".join(json.dumps(l) + "\n" for l in stats_lines))
        (work / "new_embodiment_config.py").write_text(embodiment_config_text(videos_hw))
        record = {"schema": EXPORT_SCHEMA, "codebase_version": CODEBASE_VERSION, "fps": fps,
                  "cameras": {c: VIDEO_KEYS[c] for c in cameras},
                  "vector": {"names": VECTOR_NAMES, "slices": modality_slices()},
                  "state": "measured (robot.q, robot.hand_q, waist), linear within "
                           f"{FRESH_S} s, never invented",
                  "action": "commanded (`sent`), zero-order hold",
                  "sessions": sessions, "episodes": records,
                  "total_episodes": len(episodes), "total_frames": total}
        (work / "wendy-export.json").write_text(json.dumps(record, indent=2) + "\n")
        if output.exists():
            shutil.rmtree(output)
        os.replace(work, output)
        return record
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("inputs", nargs="+", help="session directories or *.manifest.json files")
    ap.add_argument("--out", required=True, help="dataset directory to write")
    ap.add_argument("--task", help="language instruction for every episode "
                                   "(default: each session's header `task`)")
    ap.add_argument("--fps", type=int, default=DEFAULT_FPS)
    ap.add_argument("--cameras", default="left,right",
                    help="comma-separated, from left,right,head (default: left,right)")
    ap.add_argument("--outcome", action="append",
                    help="only attempts with this outcome (success, fail, abort); repeat for "
                         "several. Default: every attempt, labelled or not")
    ap.add_argument("--action-from-targets", action="store_true")
    ap.add_argument("--allow-missing-hands", action="store_true")
    ap.add_argument("--max-held", type=float, default=MAX_HELD_FRAC)
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args(argv)
    cameras = tuple(c.strip() for c in a.cameras.split(",") if c.strip())
    unknown = [c for c in cameras if c not in VIDEO_KEYS]
    if unknown:
        ap.error(f"unknown camera(s) {unknown}; choose from {sorted(VIDEO_KEYS)}")
    try:
        rec = export(find_manifests(a.inputs), a.out, task=a.task, fps=a.fps, cameras=cameras,
                     outcomes=set(a.outcome) if a.outcome else None,
                     action_from_targets=a.action_from_targets,
                     allow_missing_hands=a.allow_missing_hands, max_held=a.max_held,
                     overwrite=a.overwrite)
    except ExportRefused as e:
        print("export refused; nothing was written:", file=sys.stderr)
        for r in e.reasons:
            print(f"  {r}", file=sys.stderr)
        return 1
    print(f"{a.out}: {rec['total_episodes']} episodes, {rec['total_frames']} frames at {a.fps} fps, "
          f"cameras {', '.join(rec['cameras'].values()) or 'none'}")
    for ep in rec["episodes"]:
        print(f"  episode {ep['episode_index']}: {ep['session']} attempt {ep['attempt']} "
              f"({ep['label']}), {ep['frames']} frames, held {ep['held_frames']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
