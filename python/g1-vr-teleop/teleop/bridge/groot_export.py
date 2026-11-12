"""Export sealed teleop sessions as a GR00T / LeRobot training dataset.

    sessions (sealed .jsonl + .manifest.json + .frames/)
        -> one attempt = one episode (ARMED rows only, >= 2 s)
        -> resampled onto a fixed grid (default 15 fps)
        -> LeRobot v2.1 layout + meta/modality.json   ("GR00T LeRobot")
        -> optionally LeRobot v3.0 via LeRobot's own converter (tools/export_dataset.py)

Why v2.1 is the one written here. NVIDIA's GR00T trains on "GR00T LeRobot":
the LeRobot v2 layout (one parquet and one mp4 per episode, meta/*.jsonl) plus
meta/modality.json naming the slices of the state and action vectors, and its
loader requires v2 (it converts v3 back with convert_v3_to_v2.py). LeRobot
itself is at v3.0 and ships an official v2.1 -> v3.0 converter, which needs
meta/episodes_stats.jsonl -- written here from the exported data. So one honest
v2.1 export feeds both, and v3.0 is produced by LeRobot's code, not a
re-implementation of its layout.

Rules, from the conference build this was cut from (g1-vr-teleop, tag
avangrid-conference, bridge/groot_export.py): a manifest is verified before
anything is read; only `robot.state == "ARMED"` rows are demonstration; one
episode per attempt (episode_check.attempts: split at pauses and holds); a
measurement is never invented -- a hole refuses the attempt; and nothing is
written until every attempt has passed, so a refusal leaves no half-dataset.

Success/fail labels are optional. The recorder has no live outcome buttons;
review labels go in <sid>.labels.json beside the session (offline_labels.py,
bound to the sealed episode's SHA-256), and --outcomes success then exports
only the attempts labelled a success. The default exports every attempt.

GR00T computes meta/stats.json and meta/relative_stats.json with its own
gr00t/data/stats.py, so those are not written; LeRobot's v3 converter
aggregates meta/episodes_stats.jsonl into its own meta/stats.json.

Runs on a laptop, not the robot: numpy, pyarrow, Pillow and the system
ffmpeg/ffprobe (tools/requirements-export.txt).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import episode_check  # noqa: E402
import episode_seal  # noqa: E402
import offline_labels  # noqa: E402

# The joint orders of retarget_g1 (ARM_LEFT/ARM_RIGHT: the onboard's robot.q;
# DEX3_LEFT/DEX3_RIGHT: robot.hand_q), copied rather than imported:
# retarget_g1 parses the Unitree URDF at import, which a laptop exporting a
# dataset does not have. tests/test_export_groot.py pins them equal.
ARM_LEFT = ["left_shoulder_pitch_joint", "left_shoulder_roll_joint", "left_shoulder_yaw_joint",
            "left_elbow_joint", "left_wrist_roll_joint", "left_wrist_pitch_joint", "left_wrist_yaw_joint"]
ARM_RIGHT = [n.replace("left", "right") for n in ARM_LEFT]
DEX3_LEFT = ["left_hand_thumb_0_joint", "left_hand_thumb_1_joint", "left_hand_thumb_2_joint",
             "left_hand_middle_0_joint", "left_hand_middle_1_joint",
             "left_hand_index_0_joint", "left_hand_index_1_joint"]
DEX3_RIGHT = [n.replace("left", "right") for n in DEX3_LEFT]

EXPORT_SCHEMA = "wendy.g1.groot-export.v2"
CODEBASE_VERSION = "v2.1"
GROOT_DOC_REVISION = ("NVIDIA/Isaac-GR00T getting_started/data_preparation.md "
                      "(LeRobot v2 required; v3 converted back), read 2026-11-12")
VIDEO_KEY = "ego_view"
VIDEO_FEATURE = f"observation.images.{VIDEO_KEY}"
ANNOTATION_KEY = "human.task_description"
ANNOTATION_COLUMN = f"annotation.{ANNOTATION_KEY}"
DEFAULT_ROBOT_TYPE = "unitree_g1_29_upper_body_dex3"
DEFAULT_FPS = 15
MIN_ATTEMPT_S = 2.0          # shorter than this is a twitch, not a demonstration
FRESH_S = 0.1                # a measurement older than this is not this grid point's
CAM_FRESH_S = 0.1            # a frame older than this at a grid point is held, and counted
MAX_HELD_FRAC = 0.2          # more held frames than this and the video lies about motion
CHUNK_SIZE = 1000
# Unitree motor 12 is waist yaw (onboard/g1_onboard.py WAIST_MOTOR). Hard-coded
# so this module needs neither aiohttp nor the SDK; tests pin the two equal.
WAIST_MOTOR = 12
# LeRobot samples this many frames per episode for image statistics
# (lerobot.datasets.compute_stats.sample_indices: 100 at most, 10 at least).
STATS_IMAGE_SAMPLES = 100

# The concatenated vector, in order: waist yaw first (GR00T's own G1 configs
# lead with it), then the 14 arm joints in the onboard's `robot.q` order, then
# both Dex3 hands in `robot.hand_q` order. 29 dims, the same layout for state
# and action so a RELATIVE action has a like-named state to refer to.
GROUPS: list[tuple[str, list[str]]] = [
    ("waist", ["waist_yaw_joint"]),
    ("left_arm", list(ARM_LEFT)),
    ("right_arm", list(ARM_RIGHT)),
    ("left_hand", list(DEX3_LEFT)),
    ("right_hand", list(DEX3_RIGHT)),
]
VECTOR_NAMES: list[str] = [n for _, names in GROUPS for n in names]
VECTOR_DIM = len(VECTOR_NAMES)   # 29


def modality_slices() -> dict[str, dict[str, int]]:
    out, start = {}, 0
    for key, names in GROUPS:
        out[key] = {"start": start, "end": start + len(names)}
        start += len(names)
    return out


class ExportRefused(ValueError):
    """Nothing is written. `reasons` lists every reason, not just the first."""

    def __init__(self, reasons):
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


# ---- reading a sealed episode ---------------------------------------------------

def _read_rows(path):
    import gzip
    op = gzip.open if str(path).endswith(".gz") else open
    with op(path, "rt") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def _extract_frames_tar(base, sid):
    """<sid>.frames.tar beside the episode -> <sid>.frames/ beside it.

    /session/frames hands the operator one tar; making them untar it by hand
    before every export is a step that gets skipped. Extracted in place, so
    the seal's camera digests can be checked against the result exactly as
    against a directory the recorder wrote.
    """
    tar_path = os.path.join(base, f"{sid}.frames.tar")
    d = os.path.join(base, f"{sid}.frames")
    if os.path.isdir(d) or not os.path.isfile(tar_path):
        return False
    with tarfile.open(tar_path) as tf:
        members = tf.getmembers()
        # Refuse anything that would land outside <sid>.frames/.
        for m in members:
            if not (m.name == f"{sid}.frames" or m.name.startswith(f"{sid}.frames/")) or ".." in m.name.split("/"):
                raise ExportRefused([f"{sid}.frames.tar contains {m.name!r}, not under {sid}.frames/"])
        # filter="data" (3.12+) also refuses links and device nodes.
        try:
            tf.extractall(base, filter="data")
        except TypeError:
            tf.extractall(base)
    return True


def load(manifest_path) -> tuple[dict, dict]:
    """Verify, then read. -> (manifest, episode) where episode has header,
    frames (untyped rows), marks, outcomes, footer. Raises ExportRefused."""
    manifest_path = os.path.abspath(str(manifest_path))
    base = os.path.dirname(manifest_path)
    try:
        with open(manifest_path, "rt") as fh:
            m = json.load(fh)
    except (OSError, ValueError) as e:
        raise ExportRefused([f"{os.path.basename(manifest_path)}: manifest unreadable: {e}"])
    sid = m.get("session") or os.path.basename(manifest_path)[:-len(".manifest.json")]
    if (m.get("camera") or {}).get("dir"):
        _extract_frames_tar(base, sid)
    rep = episode_seal.verify(manifest_path)
    if not rep["ok"]:
        raise ExportRefused([f"{rep['path']}: {r}" for r in rep["reasons"]])
    ep_path = os.path.join(base, (m.get("episode") or {}).get("file") or "")
    try:
        reviewed = offline_labels.load(ep_path, episode_sha256=(m.get("episode") or {}).get("sha256"))
    except (OSError, ValueError) as exc:
        raise ExportRefused([f"{sid}: offline labels invalid: {exc}"])
    header, body, footer, marks, outcomes = episode_check.load(ep_path)
    # Review labels live beside the sealed file, never in it; merged only now,
    # after the seal has been verified and the sidecar bound to its digest.
    marks = sorted(marks + reviewed["marks"], key=lambda r: float(r["t"]))
    outcomes = sorted(outcomes + reviewed["outcomes"], key=lambda r: float(r["t"]))
    labels = os.path.basename(reviewed["path"]) if os.path.isfile(reviewed["path"]) else None
    return m, {"header": header, "footer": footer, "frames": body, "marks": marks,
               "outcomes": outcomes, "path": ep_path, "labels": labels}


def attempts(episode) -> list[dict]:
    """The producer's own attempt rule (episode_check.attempts): split at marks
    and >1 s pauses, outcomes attached to the containing or most recently
    finished attempt. Returned with an index and the row span."""
    frames = episode["frames"]
    atts = episode_check.attempts(frames, episode["marks"], outcomes=episode["outcomes"])
    for i, a in enumerate(atts):
        a["index"] = i
        a["rows"] = [r for r in frames if a["t0"] <= float(r["t"]) <= a["t1"]]
    return atts


# ---- resampling ------------------------------------------------------------------

def _zoh(sample_t, sample_v, grid_t):
    """Zero-order hold: the last sample at or before each grid point; NaN
    before the first. sample_v is (n, k)."""
    out = np.full((len(grid_t), sample_v.shape[1]), np.nan)
    if len(sample_t) == 0:
        return out
    idx = np.searchsorted(sample_t, grid_t, side="right") - 1
    ok = idx >= 0
    out[ok] = sample_v[idx[ok]]
    return out


def _linear_fresh(sample_t, sample_v, grid_t, fresh_s=FRESH_S):
    """Linear interpolation between the bracketing samples, but only where a
    sample exists within fresh_s of the grid point. Elsewhere NaN: a
    measurement is never invented, and the count of such points is reported."""
    out = np.full((len(grid_t), sample_v.shape[1]), np.nan)
    if len(sample_t) == 0:
        return out
    pos = np.searchsorted(sample_t, grid_t)
    lo = np.clip(pos - 1, 0, len(sample_t) - 1)
    hi = np.clip(pos, 0, len(sample_t) - 1)
    d_lo = np.abs(grid_t - sample_t[lo])
    d_hi = np.abs(sample_t[hi] - grid_t)
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
    """(t, vector) samples from rows via pick(row)->list|None, sorted and
    de-duplicated on t: the onboard repeats a measurement between lowstate
    updates, and one sample per measurement instant is what interpolation
    wants."""
    ts, vs, seen = [], [], set()
    for r in rows:
        v = pick(r)
        if v is None:
            continue
        t = t_of(r)
        if t is None or t in seen:
            continue
        seen.add(t)
        ts.append(t)
        vs.append(v)
    if not ts:
        return np.zeros(0), np.zeros((0, 1))
    order = np.argsort(ts)
    return np.asarray(ts, float)[order], np.asarray(vs, float)[order]


# ---- per-row pickers ------------------------------------------------------------

def _t_meas(row):
    rb = row.get("robot") or {}
    age = rb.get("q_age_s")
    return float(row["t"]) - (float(age) if isinstance(age, (int, float)) else 0.0)


def _waist_q(row):
    """Measured waist yaw: body state motor 12 when present, else the waist ack."""
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
    if not isinstance(h, dict):
        return None
    v = h.get(side)
    return [float(x) for x in v] if isinstance(v, list) and len(v) == 7 else None


def _action_row(row, names, allow_targets):
    """Commanded value for `names` at this row: `sent` (what the robot was
    told, after the arming offset) when it carries them all, else `targets`
    only when the caller allowed that fallback. -> (values, source) or None."""
    s = row.get("sent")
    if isinstance(s, dict) and all(isinstance(s.get(n), (int, float)) for n in names):
        return [float(s[n]) for n in names], "sent"
    if allow_targets:
        t = row.get("targets") or {}
        if all(isinstance(t.get(n), (int, float)) for n in names):
            return [float(t[n]) for n in names], "targets"
    return None



# ---- one attempt -> one episode -------------------------------------------------

def build_episode(attempt, fps, *, action_from_targets=False, allow_missing_hands=False,
                  fresh_s=FRESH_S) -> dict:
    """Resample one attempt's ARMED rows onto a fixed grid. Raises ExportRefused
    listing every reason the attempt cannot be exported."""
    rows = [r for r in attempt["rows"] if (r.get("robot") or {}).get("state") == "ARMED"]
    label = f"attempt {attempt['index']} ({attempt['label']})"
    reasons: list[str] = []
    if len(rows) < 2:
        raise ExportRefused([f"{label}: fewer than two ARMED rows"])
    t_rows = np.asarray([float(r["t"]) for r in rows])
    if t_rows[-1] - t_rows[0] < MIN_ATTEMPT_S:
        reasons.append(f"{label}: only {t_rows[-1] - t_rows[0]:.2f} s ARMED, under {MIN_ATTEMPT_S:.0f} s")
    grid = np.arange(t_rows[0], t_rows[-1] + 1e-9, 1.0 / fps)

    # ---- state: measured, linearly interpolated, never invented ----
    state = np.full((len(grid), VECTOR_DIM), np.nan)
    caveats: list[str] = []
    zero_filled: list[str] = []
    sl = modality_slices()

    def fill(group, pick):
        s_t, s_v = _samples(rows, pick, _t_meas)
        if len(s_t):
            state[:, sl[group]["start"]:sl[group]["end"]] = _linear_fresh(s_t, s_v, grid, fresh_s)

    fill("waist", _waist_q)
    fill("left_arm", lambda r: (_arm_q(r) or [None] * 14)[:7] if _arm_q(r) else None)
    fill("right_arm", lambda r: (_arm_q(r) or [None] * 14)[7:] if _arm_q(r) else None)
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
                               "pass --allow-missing-hands to zero-fill and record it")
    bad = [VECTOR_NAMES[j] for j in range(VECTOR_DIM) if np.isnan(state[:, j]).any()]
    if bad:
        holes = int(np.isnan(state).any(axis=1).sum())
        reasons.append(f"{label}: no fresh measurement within {fresh_s:.2f} s at {holes} of "
                       f"{len(grid)} grid points for {', '.join(bad[:6])}"
                       + (" ..." if len(bad) > 6 else ""))

    # ---- action: commanded, zero-order hold ----
    action = np.full((len(grid), VECTOR_DIM), np.nan)
    arm_src: set[str] = set()
    for group, names in (("left_arm", ARM_LEFT), ("right_arm", ARM_RIGHT)):
        ts, vs = [], []
        for r in rows:
            got = _action_row(r, names, action_from_targets)
            if got is None:
                continue
            ts.append(float(r["t"]))
            vs.append(got[0])
            arm_src.add(got[1])
        if ts:
            action[:, sl[group]["start"]:sl[group]["end"]] = _zoh(np.asarray(ts), np.asarray(vs), grid)
    if not arm_src:
        reasons.append(f"{label}: no arm command -- rows carry no `sent`; pass --action-from-targets "
                       "to use the pre-offset targets (recorded as a caveat)")
    elif arm_src == {"targets"}:
        caveats.append("arm action taken from pre-offset `targets`, not the command sent (--action-from-targets)")
    elif len(arm_src) > 1:
        caveats.append("arm action mixes `sent` and pre-offset `targets` rows")
    # The waist has no arming offset and the hands are never offset, so their
    # raw targets ARE what was sent; `sent` is preferred when it carries them.
    for group, names in (("waist", ["waist_yaw_joint"]), ("left_hand", DEX3_LEFT), ("right_hand", DEX3_RIGHT)):
        ts, vs = [], []
        for r in rows:
            got = _action_row(r, names, True)
            if got is None:
                continue
            ts.append(float(r["t"]))
            vs.append(got[0])
        if ts:
            action[:, sl[group]["start"]:sl[group]["end"]] = _zoh(np.asarray(ts), np.asarray(vs), grid)
    bad_a = [VECTOR_NAMES[j] for j in range(VECTOR_DIM) if np.isnan(action[:, j]).any()]
    if bad_a:
        reasons.append(f"{label}: no command for {', '.join(bad_a[:6])}" + (" ..." if len(bad_a) > 6 else ""))

    if reasons:
        raise ExportRefused(reasons)
    return {
        "attempt": attempt["index"], "label": attempt["label"],
        "outcome": attempt.get("outcome"),
        "t_grid": grid, "t0": float(t_rows[0]),
        "armed_seconds": round(float(t_rows[-1] - t_rows[0]), 3),
        "armed_rows": len(rows),
        "state": state.astype(np.float32), "action": action.astype(np.float32),
        "action_source": "sent" if arm_src == {"sent"} else "targets" if arm_src == {"targets"} else "mixed",
        "zero_filled": zero_filled, "caveats": caveats,
        "rows": rows,
    }



# ---- camera frames ----------------------------------------------------------------

def read_index(frames_dir) -> dict[int, dict]:
    """<sid>.frames/index.jsonl -> {seq: entry}. Empty when there is none."""
    out = {}
    legacy = os.path.join(frames_dir, "index.jsonl")
    if os.path.isfile(legacy):
        with open(legacy, "rt") as fh:
            for line in fh:
                if not line.strip():
                    continue
                e = json.loads(line)
                if isinstance(e.get("seq"), (int, float)) and e.get("file"):
                    # `t_recv` (bridge clock) rides along: it is what freshness is
                    # judged on -- dropping it here was why every real frame read
                    # as held (frame_time_bridge fell back to the robot's capture t).
                    out[int(e["seq"])] = {"seq": int(e["seq"]), "file": e["file"], "t": e.get("t"),
                                          "t_recv": e.get("t_recv")}
    return out


def read_rgb_array(path):
    """RGB uint8 from a recorded JPEG."""
    from PIL import Image
    with Image.open(str(path)) as image:
        return np.asarray(image.convert("RGB")).copy()


def frame_time_bridge(entry):
    """The index entry's time on the BRIDGE clock: `t_recv` when the recorder
    wrote it, else the capture `t`.

    Rows are stamped by the bridge; a frame's `t` is the detector's capture
    stamp on the ROBOT's clock, and the two differ by network + detector
    latency + any clock offset -- measured at 0.18-0.29 s on 20260916-001041.
    Judging freshness with `t` against a row `t` therefore called every frame
    stale (325 of 325 "held") on a session whose bridge-side frame age was
    66 ms. `t_recv` is the same clock as the rows; that is the comparison.
    """
    for k in ("t_recv", "t"):
        v = entry.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


# A frame is stale once a newer one should have arrived: 1.5x the camera's
# own median interval (conference build, episode_check.CAM_INTERVAL_FACTOR).
CAM_INTERVAL_FACTOR = 1.5


def camera_fresh_window(index, floor_s=CAM_FRESH_S):
    """How old a frame may be at a grid point before it counts as held.

    CAM_FRESH_S alone assumes the camera runs faster than the grid. The
    detector's frames arrive at ~7.5 fps (0.133 s apart) on the real sessions
    while the grid is 15 fps, so every second grid point sat 100-133 ms after
    the newest frame that existed and was counted "held" -- 33 % on
    20260916-001041, over the 20 % ceiling, for a camera that was working. A
    frame is stale when a newer one should have come and did not: the window
    is the larger of CAM_FRESH_S and 1.5x the camera's own median interval,
    measured from the index on the bridge clock. Repeated (same frame at two
    grid points) is counted separately and is what a slow camera looks like.
    """
    ts = sorted(v for v in (frame_time_bridge(e) for e in index.values()) if v is not None)
    if len(ts) < 3:
        return float(floor_s)
    gaps = np.diff(np.asarray(ts))
    gaps = gaps[gaps > 0]
    if not len(gaps):
        return float(floor_s)
    return float(max(floor_s, CAM_INTERVAL_FACTOR * float(np.median(gaps))))


def choose_frames(ep, frames_dir, index) -> tuple[list[str], int, int]:
    """For each grid point, the camera frame current at the nearest row, by
    seq; held (previous frame kept) when that frame is older than the camera's
    fresh window at the grid point or unknown to the index. -> (files, held,
    repeated)."""
    rows = ep["rows"]
    fresh_window = camera_fresh_window(index)
    ep["camera_fresh_window_s"] = round(fresh_window, 3)
    cam_rows = [r for r in rows if isinstance(r.get("cam"), dict) and r["cam"].get("seq") is not None]
    if not cam_rows:
        raise ExportRefused([f"attempt {ep['attempt']}: rows carry no camera frame reference (`cam`)"])
    c_t = np.asarray([float(r["t"]) for r in cam_rows])
    seqs = np.asarray([[float(r["cam"]["seq"]), float(r["cam"].get("t") or np.nan)] for r in cam_rows])
    held_seq = _zoh(c_t, seqs, ep["t_grid"])
    files: list[str] = []
    held = repeated = 0
    prev_file = prev_seq = None
    for k, t in enumerate(ep["t_grid"]):
        seq = held_seq[k, 0]
        entry = index.get(int(seq)) if np.isfinite(seq) else None
        frame_t = None
        if entry is not None:
            frame_t = frame_time_bridge(entry)          # bridge clock, like the rows
            if frame_t is None and np.isfinite(held_seq[k, 1]):
                frame_t = float(held_seq[k, 1])
        fresh = entry is not None and (frame_t is None or t - frame_t <= fresh_window)
        if fresh:
            f = os.path.join(frames_dir, entry["file"])
            fresh = os.path.isfile(f)
        if fresh:
            if prev_seq is not None and int(seq) == prev_seq:
                repeated += 1
            prev_file, prev_seq = f, int(seq)
            files.append(f)
        else:
            if prev_file is None:
                later = next((index[int(s)] for s in held_seq[k:, 0] if np.isfinite(s) and int(s) in index), None)
                if later is None:
                    raise ExportRefused([f"attempt {ep['attempt']}: no camera frame can be matched to the grid"])
                prev_file = os.path.join(frames_dir, later["file"])
            held += 1
            files.append(prev_file)
    return files, held, repeated


def encode_video(files, fps, out_mp4, ffmpeg="ffmpeg") -> tuple[int, int]:
    """Concatenate JPEGs at fps into H.264 yuv420p. -> (width, height)."""
    from PIL import Image
    first = read_rgb_array(files[0])
    h, w = first.shape[:2]
    if w % 2 or h % 2:
        raise ExportRefused([f"camera frames are {w}x{h}; yuv420p needs even dimensions"])
    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="groot-frames-") as td:
        for i, f in enumerate(files):
            target = os.path.join(td, f"{i:06d}.jpg")
            if str(f).endswith(".zip"):
                Image.fromarray(read_rgb_array(f)).save(target, format="JPEG", quality=95)
            else:
                os.symlink(os.path.abspath(f), target)
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-framerate", str(fps), "-i", os.path.join(td, "%06d.jpg"),
               "-frames:v", str(len(files)),
               # A keyframe every 2 frames and no B-frames, as LeRobot encodes its
               # own (VideoEncoderConfig g=2): training reads frames at random, and
               # with a 15-frame GOP LeRobot's v3 concatenation left frames past the
               # first episode undecodable by torchcodec on random access.
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "2", "-bf", "0", "-crf", "18",
               "-movflags", "+faststart", str(out_mp4)]
        subprocess.run(cmd, check=True)
    return w, h


def count_video_frames(path, ffprobe="ffprobe") -> int:
    out = subprocess.run([ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
                          "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return int(out.split(",")[0])



def tools_available() -> tuple[bool, list[str]]:
    missing = []
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        missing.append("pyarrow (pip install pyarrow)")
    try:
        import PIL  # noqa: F401
    except ImportError:
        missing.append("Pillow")
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            missing.append(tool)
    return not missing, missing


# ---- writing the dataset ---------------------------------------------------------


# ---- writing one episode ----------------------------------------------------------

def write_parquet(path, ep, episode_index, global_start, fps):
    import pyarrow as pa
    import pyarrow.parquet as pq
    n = len(ep["t_grid"])
    # Snap to the grid exactly: LeRobot checks timestamp * fps against
    # frame_index within tolerance, and float accumulation drifts past it.
    ts = (np.arange(n) / float(fps)).astype(np.float32)
    table = pa.table({
        "observation.state": pa.array(list(ep["state"]), type=pa.list_(pa.float32())),
        "action": pa.array(list(ep["action"]), type=pa.list_(pa.float32())),
        "timestamp": pa.array(ts, type=pa.float32()),
        ANNOTATION_COLUMN: pa.array(np.zeros(n, dtype=np.int64)),
        "task_index": pa.array(np.zeros(n, dtype=np.int64)),
        "episode_index": pa.array(np.full(n, episode_index, dtype=np.int64)),
        "frame_index": pa.array(np.arange(n, dtype=np.int64)),
        "index": pa.array(np.arange(global_start, global_start + n, dtype=np.int64)),
        "next.reward": pa.array(np.zeros(n, dtype=np.float32)),
        "next.done": pa.array(np.arange(n) == n - 1),
    })
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    return n


# ---- LeRobot v2.1 per-episode statistics -------------------------------------------
#
# meta/episodes_stats.jsonl: one line per episode, {"episode_index", "stats":
# {feature: {min, max, mean, std, count, q01, q10, q50, q90, q99}}}, the shapes
# lerobot.datasets.compute_stats.compute_episode_stats produces: a vector
# feature reduces over time to its own shape, a scalar keeps shape [1], and a
# video is per channel, shape [3, 1, 1], scaled to [0, 1] from sampled frames,
# with count = the episode's length. LeRobot's v2.1 -> v3.0 converter reads
# this file and aggregates it into meta/stats.json. Quantiles here are exact
# (numpy) where LeRobot's are histogram estimates of the same quantity.

QUANTILES = (("q01", 0.01), ("q10", 0.10), ("q50", 0.50), ("q90", 0.90), ("q99", 0.99))


def _stats(arr, axis, keepdims):
    arr = np.asarray(arr, dtype=np.float64)
    out = {"min": arr.min(axis=axis, keepdims=keepdims), "max": arr.max(axis=axis, keepdims=keepdims),
           "mean": arr.mean(axis=axis, keepdims=keepdims), "std": arr.std(axis=axis, keepdims=keepdims)}
    for name, q in QUANTILES:
        out[name] = np.quantile(arr, q, axis=axis, keepdims=keepdims)
    return out


def sample_indices(n) -> list[int]:
    """LeRobot's own rule for which frames image statistics come from."""
    k = max(min(n, 100), min(int(n ** 0.75), 10_000))
    return np.round(np.linspace(0, n - 1, k)).astype(int).tolist()


def episode_stats(ep, n, fps, episode_index, global_start, video_files=None) -> dict:
    cols: dict[str, np.ndarray] = {
        "observation.state": ep["state"][:n], "action": ep["action"][:n],
        "timestamp": (np.arange(n) / float(fps)).astype(np.float32),
        ANNOTATION_COLUMN: np.zeros(n, dtype=np.int64),
        "task_index": np.zeros(n, dtype=np.int64),
        "episode_index": np.full(n, episode_index, dtype=np.int64),
        "frame_index": np.arange(n, dtype=np.int64),
        "index": np.arange(global_start, global_start + n, dtype=np.int64),
        "next.reward": np.zeros(n, dtype=np.float32),
        "next.done": (np.arange(n) == n - 1).astype(np.float64),
    }
    stats = {}
    for key, arr in cols.items():
        s = _stats(arr, 0, arr.ndim == 1)
        s["count"] = np.array([n])
        stats[key] = s
    if video_files is not None:
        imgs = np.stack([read_rgb_array(video_files[i]) for i in sample_indices(len(video_files))])
        chw = imgs.transpose(0, 3, 1, 2).astype(np.float64) / 255.0       # (k, 3, h, w)
        s = _stats(chw, (0, 2, 3), True)                                 # (1, 3, 1, 1)
        s = {k: v.reshape(3, 1, 1) for k, v in s.items()}
        s["count"] = np.array([n])
        stats[VIDEO_FEATURE] = s
    return {"episode_index": episode_index,
            "stats": {k: {m: np.asarray(v).tolist() for m, v in s.items()} for k, s in stats.items()}}


# ---- meta files -------------------------------------------------------------------

def info_json(fps, robot_type, total_episodes, total_frames, video_hw) -> dict:
    scalar = lambda dtype: {"dtype": dtype, "shape": [1], "names": None}  # noqa: E731
    features: dict[str, Any] = {
        "observation.state": {"dtype": "float32", "shape": [VECTOR_DIM], "names": list(VECTOR_NAMES)},
        "action": {"dtype": "float32", "shape": [VECTOR_DIM], "names": list(VECTOR_NAMES)},
    }
    if video_hw is not None:
        h, w = video_hw
        features[VIDEO_FEATURE] = {
            "dtype": "video", "shape": [h, w, 3], "names": ["height", "width", "channels"],
            "info": {"video.height": h, "video.width": w, "video.codec": "h264",
                     "video.pix_fmt": "yuv420p", "video.is_depth_map": False,
                     "video.fps": fps, "video.channels": 3, "has_audio": False},
        }
    features.update({
        "timestamp": scalar("float32"),
        "frame_index": scalar("int64"),
        "episode_index": scalar("int64"),
        "index": scalar("int64"),
        "task_index": scalar("int64"),
        ANNOTATION_COLUMN: scalar("int64"),
        "next.reward": scalar("float32"),
        "next.done": scalar("bool"),
    })
    return {
        "codebase_version": CODEBASE_VERSION,
        "robot_type": robot_type,
        "total_episodes": total_episodes,
        "total_frames": total_frames,
        "total_tasks": 1,
        "total_videos": total_episodes if video_hw is not None else 0,
        "total_chunks": (total_episodes + CHUNK_SIZE - 1) // CHUNK_SIZE,
        "chunks_size": CHUNK_SIZE,
        "fps": fps,
        "splits": {"train": f"0:{total_episodes}"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": features,
    }


def modality_json(with_video=True) -> dict:
    sl = modality_slices()
    out: dict[str, Any] = {"state": sl, "action": dict(sl),
                           "annotation": {ANNOTATION_KEY: {"original_key": "task_index"}}}
    if with_video:
        out["video"] = {VIDEO_KEY: {"original_key": VIDEO_FEATURE}}
    return out


# GR00T's data config for this embodiment, written beside the dataset. Text, not
# code this package runs: it imports gr00t, which only the training machine has.
EMBODIMENT_CONFIG = '''"""GR00T modality config for the Wendy G1 upper body + Dex3 dataset.

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
    "video": ModalityConfig(delta_indices=[0], modality_keys=["__VIDEO_KEY__"]),
    "state": ModalityConfig(
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
    "language": ModalityConfig(delta_indices=[0], modality_keys=["__ANNOTATION_COLUMN__"]),
}

MODALITY_CONFIGS.pop(EmbodimentTag.NEW_EMBODIMENT.value, None)
register_modality_config(config, embodiment_tag=EmbodimentTag.NEW_EMBODIMENT)
'''


def embodiment_config_text(with_video=True) -> str:
    text = EMBODIMENT_CONFIG.replace("__VIDEO_KEY__", VIDEO_KEY).replace(
        "__ANNOTATION_COLUMN__", ANNOTATION_COLUMN)
    if not with_video:
        text = text.replace(f'    "video": ModalityConfig(delta_indices=[0], modality_keys=["{VIDEO_KEY}"]),\n',
                            "    # no video: this dataset was exported with --allow-missing-frames\n")
    return text


# ---- the export -------------------------------------------------------------------

def plan(manifest_paths, *, outcomes, action_from_targets, allow_missing_hands,
         allow_missing_frames, fps, fresh_s=FRESH_S) -> tuple[list[dict], list[dict], list[str]]:
    """Verify, read, split, resample. Nothing is written. -> (sources, episodes,
    per-attempt refusals). Raises ExportRefused for anything that stops the run."""
    fatal: list[str] = []
    sources: list[dict] = []
    episodes: list[dict] = []
    skipped: list[str] = []
    for mp in manifest_paths:
        try:
            manifest, episode = load(mp)
        except ExportRefused as e:
            fatal.extend(e.reasons)
            continue
        base = os.path.dirname(os.path.abspath(mp))
        cam = manifest.get("camera")
        frames_dir = os.path.join(base, cam["dir"]) if cam and cam.get("dir") else None
        sid = manifest.get("session")
        unsealed = os.path.join(base, f"{sid}.frames") if frames_dir is None else None
        if unsealed is not None and not os.path.isdir(unsealed):
            unsealed = None
        sources.append({
            "manifest": os.path.abspath(mp), "session": sid,
            "identity": {"schema": manifest.get("schema"), "session": sid,
                         "episode_sha256": (manifest.get("episode") or {}).get("sha256"),
                         "camera_frames_sha256": (cam or {}).get("frames_sha256"),
                         "camera_index_sha256": (cam or {}).get("index_sha256")},
            "source": manifest.get("source"),
            "task": (episode.get("header") or {}).get("task"),
            "labels": episode.get("labels"),
        })
        atts = attempts(episode)
        if frames_dir is None and unsealed is None and not allow_missing_frames:
            skipped.append(f"{sid}: no camera frames sealed with this session (record with RECORDING=on "
                           "and pull them with tools/pull_sessions.py); GR00T and LeRobot need video")
        wanted = [a for a in atts if outcomes is None or (a.get("outcome") or {}).get("result") in outcomes]
        if not wanted:
            have = sorted({(a.get("outcome") or {}).get("result") or "no outcome" for a in atts})
            skipped.append(f"{sid}: 0 of {len(atts)} attempts with outcome in "
                           f"{sorted(outcomes) if outcomes else 'any'} (attempts have: {', '.join(have)})")
        for a in wanted:
            try:
                ep = build_episode(a, fps, action_from_targets=action_from_targets,
                                   allow_missing_hands=allow_missing_hands, fresh_s=fresh_s)
            except ExportRefused as e:
                skipped.extend(f"{sid}: {r}" for r in e.reasons)
                continue
            ep["session"] = sid
            if frames_dir is None:
                if unsealed:
                    skipped.append(f"{sid}: camera frames exist at {os.path.basename(unsealed)} but the "
                                   "manifest was sealed without them; reseal the session (they are unverified)")
                    continue
                if not allow_missing_frames:
                    continue
                ep["caveats"].append("no camera frames: exported without video (--allow-missing-frames)")
                ep["files"], ep["held_frames"], ep["repeated_frames"] = None, 0, 0
            else:
                try:
                    index = read_index(frames_dir)
                    files, held, repeated = choose_frames(ep, frames_dir, index)
                except (OSError, ValueError, ExportRefused) as e:
                    skipped.append(f"{sid}: attempt {a['index']}: {e}")
                    continue
                if held > MAX_HELD_FRAC * len(files):
                    skipped.append(f"{sid}: attempt {a['index']}: {held} of {len(files)} video frames would be "
                                   f"held from an older frame ({100 * held / len(files):.0f} % > "
                                   f"{100 * MAX_HELD_FRAC:.0f} %)")
                    continue
                ep["files"], ep["held_frames"], ep["repeated_frames"] = files, held, repeated
            episodes.append(ep)
    if fatal:
        raise ExportRefused(fatal + skipped)
    return sources, episodes, skipped


def export(manifest_paths, output, task, *, fps=DEFAULT_FPS, outcomes=None,
           robot_type=DEFAULT_ROBOT_TYPE, action_from_targets=False, allow_missing_hands=False,
           allow_missing_frames=False, ffmpeg="ffmpeg", ffprobe="ffprobe") -> dict:
    """Write one GR00T LeRobot v2.1 dataset from one or more sealed manifests.

    `task` None means: take it from the sessions' headers, which must agree.
    `outcomes` None exports every attempt; a set keeps only attempts whose
    offline label's result is in it.
    """
    output = Path(output)
    reasons: list[str] = []
    if not (isinstance(fps, int) and 1 <= fps <= 30):
        reasons.append(f"--fps must be a whole number in 1..30, got {fps!r}")
    if output.exists():
        reasons.append(f"output already exists: {output} (datasets are created, never overwritten)")
    ok, missing = tools_available()
    if not ok and not (allow_missing_frames and all(m.startswith(("ffmpeg", "ffprobe")) for m in missing)):
        reasons.append("missing: " + ", ".join(missing))
    if task is not None and not str(task).strip():
        reasons.append("--task is empty: every episode carries one language instruction")
    if reasons:
        raise ExportRefused(reasons)
    sources, episodes, skipped = plan(manifest_paths, outcomes=outcomes,
                                      action_from_targets=action_from_targets,
                                      allow_missing_hands=allow_missing_hands,
                                      allow_missing_frames=allow_missing_frames, fps=fps)
    if task is None:
        # The header's task: recorded at session start from the console's task
        # box, ?task= or TELEOP_TASK. One dataset carries one instruction, so
        # the sessions must agree; disagreement is a --task decision.
        tasks = {str(s["task"]).strip() for s in sources if s.get("task") and str(s["task"]).strip()}
        if len(tasks) == 1:
            task = tasks.pop()
        elif not tasks:
            raise ExportRefused(["no task: the session header carries none (type it on the console at "
                                 "Start session, or set the TASK template variable) and --task was not given"]
                                + skipped)
        else:
            raise ExportRefused([f"the sessions carry different tasks {sorted(tasks)}; pass --task to choose"]
                                + skipped)
    task = str(task).strip()
    if not episodes:
        raise ExportRefused(["nothing to export"] + skipped)
    with_video = any(ep["files"] is not None for ep in episodes)
    if with_video and not all(ep["files"] is not None for ep in episodes):
        raise ExportRefused(["a dataset cannot mix episodes with and without video; "
                             "drop --allow-missing-frames or the episodes lacking frames"] + skipped)

    # Write. Everything above was checked; from here a failure is an I/O error,
    # and the half-written directory is removed so a retry starts clean.
    output.mkdir(parents=True, exist_ok=False)
    try:
        record = _write(output, sources, episodes, skipped, task, fps, robot_type, with_video,
                        outcomes, ffmpeg, ffprobe)
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return record


def _write(output, sources, episodes, skipped, task, fps, robot_type, with_video, outcomes, ffmpeg, ffprobe):
    (output / "meta").mkdir()
    total_frames = 0
    video_hw = None
    ep_records = []
    with open(output / "meta" / "episodes.jsonl", "wt") as ej, \
            open(output / "meta" / "episodes_stats.jsonl", "wt") as es:
        for i, ep in enumerate(episodes):
            chunk = i // CHUNK_SIZE
            n = write_parquet(output / "data" / f"chunk-{chunk:03d}" / f"episode_{i:06d}.parquet",
                              ep, i, total_frames, fps)
            vid = None
            if ep["files"] is not None:
                mp4 = output / "videos" / f"chunk-{chunk:03d}" / VIDEO_FEATURE / f"episode_{i:06d}.mp4"
                w, h = encode_video(ep["files"], fps, mp4, ffmpeg=ffmpeg)
                got = count_video_frames(mp4, ffprobe=ffprobe)
                if got != n:
                    raise ExportRefused([f"episode {i}: video has {got} frames but parquet has {n} rows"])
                if video_hw is None:
                    video_hw = (h, w)
                elif video_hw != (h, w):
                    raise ExportRefused([f"episode {i}: frame size {w}x{h} differs from {video_hw[1]}x{video_hw[0]}"])
                vid = str(mp4.relative_to(output))
            ej.write(json.dumps({"episode_index": i, "tasks": [task], "length": n}) + "\n")
            es.write(json.dumps(episode_stats(ep, n, fps, i, total_frames, ep["files"])) + "\n")
            ep_records.append({
                "episode_index": i, "session": ep["session"], "attempt": ep["attempt"],
                "label": ep["label"], "outcome": ep["outcome"], "armed_seconds": ep["armed_seconds"],
                "armed_rows": ep["armed_rows"], "frames": n,
                "held_camera_frames": ep["held_frames"], "repeated_camera_frames": ep["repeated_frames"],
                "camera_fresh_window_s": ep.get("camera_fresh_window_s"),
                "video": vid, "action_source": ep["action_source"],
                "zero_filled_groups": ep["zero_filled"], "caveats": ep["caveats"],
                "t0_unix": ep["t0"],
            })
            total_frames += n
    with open(output / "meta" / "tasks.jsonl", "wt") as tj:
        tj.write(json.dumps({"task_index": 0, "task": task}) + "\n")
    (output / "meta" / "info.json").write_text(
        json.dumps(info_json(fps, robot_type, len(episodes), total_frames, video_hw), indent=2) + "\n")
    (output / "meta" / "modality.json").write_text(json.dumps(modality_json(with_video), indent=2) + "\n")
    (output / "new_embodiment_config.py").write_text(embodiment_config_text(with_video))
    record = {
        "schema": EXPORT_SCHEMA,
        "groot_format": {"codebase_version": CODEBASE_VERSION, "doc_revision": GROOT_DOC_REVISION,
                         "embodiment_tag": "new_embodiment", "video_key": VIDEO_KEY if with_video else None,
                         "annotation_column": ANNOTATION_COLUMN,
                         "modality_config": "new_embodiment_config.py"},
        "lerobot_format": {"codebase_version": CODEBASE_VERSION, "episodes_stats": "meta/episodes_stats.jsonl",
                           "v3": "tools/export_dataset.py --format lerobot-v3 runs LeRobot's own "
                                 "convert_dataset_v21_to_v30 on a copy"},
        "task": task, "fps": fps, "robot_type": robot_type,
        "vector": {"names": VECTOR_NAMES, "groups": modality_slices(), "units": "radians",
                   "state": "measured joint positions (robot.q, robot.hand_q, waist from body state), "
                            "linearly interpolated between fresh samples, never extrapolated",
                   "action": "the command sent to the robot (row `sent`, after the arming offset), "
                             "zero-order held"},
        "conventions": {"next.done": "true on the last frame of each episode", "next.reward": "0.0 (unused)",
                        "timestamp": "frame_index / fps, seconds from the attempt's first ARMED row",
                        "episode": "one attempt (episode_check.attempts), ARMED rows only, at least "
                                   f"{MIN_ATTEMPT_S:.0f} s",
                        "held_camera_frames": f"grid points whose freshest frame was too old; "
                                              f"refused above {int(MAX_HELD_FRAC * 100)} %"},
        "outcomes_exported": sorted(outcomes) if outcomes else "all",
        "sources": [s | {"verified": True} for s in sources],
        "episodes": ep_records,
        "skipped": skipped,
        "stats_note": "GR00T: python gr00t/data/stats.py --dataset-path <out> --embodiment-tag new_embodiment "
                      "--modality-config-path <out>/new_embodiment_config.py writes meta/stats.json and "
                      "meta/relative_stats.json. LeRobot v3: the converter aggregates episodes_stats.jsonl.",
    }
    (output / "wendy-export.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record


def summary_line(record) -> str:
    eps = record["episodes"]
    lines = [f"GR00T LeRobot {record['groot_format']['codebase_version']}: {len(eps)} episode(s), "
             f"{sum(e['frames'] for e in eps)} frames @ {record['fps']} fps, "
             f"{'with' if record['groot_format']['video_key'] else 'WITHOUT'} video, task {record['task']!r}"]
    for e in eps:
        oc = (e.get("outcome") or {}).get("result") or "no outcome"
        lines.append(f"  episode {e['episode_index']}: {e['session']} attempt {e['attempt']} ({e['label']}), {oc}, "
                     f"{e['armed_seconds']} s ARMED -> {e['frames']} frames, held cam {e['held_camera_frames']}, "
                     f"action from {e['action_source']}"
                     + (f", caveats: {'; '.join(e['caveats'])}" if e["caveats"] else ""))
    for s in record["skipped"]:
        lines.append(f"  skipped: {s}")
    return "\n".join(lines)
