"""Recorder settings from environment variables (set with `wendy run --env`).

Standard library only, so the repository tests can check it without LeRobot.
The README lists every variable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from so101_common import (
    DEFAULT_TASK,
    SettingsError,
    camera_fourcc,
    env_bool,
    env_float,
    env_int,
    env_str,
    parse_cameras,
    serial_port,
)

DEFAULT_REPO_ID = "local/so101_demos"
# LeRobot's own default codec is libsvtav1 (AV1). h264 is the fallback for a
# device that is short of CPU.
VIDEO_CODECS = ("libsvtav1", "h264", "hevc")
PUSH_MODES = ("auto", "true", "false")
# Hugging Face repo ids: <namespace>/<name>, letters, digits, '-', '_' and '.'.
REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


@dataclass(frozen=True)
class RecordSettings:
    follower_port: str
    leader_port: str
    follower_calibration: Path
    leader_calibration: Path
    cameras: dict
    camera_width: int
    camera_height: int
    camera_fourcc: str | None
    fps: int
    task: str
    num_episodes: int
    episode_seconds: float
    reset_seconds: float
    repo_id: str
    dataset_root: Path
    push: bool
    push_reason: str
    private: bool
    use_degrees: bool
    video_codec: str
    streaming_encoding: bool
    encoder_threads: int
    http_port: int
    dry_run: bool


def check_repo_id(repo_id, variable="SO101_DATASET_REPO_ID"):
    if not REPO_ID.match(repo_id) or ".." in repo_id or "--" in repo_id:
        raise SettingsError(
            f"{variable}={repo_id!r} is not a Hugging Face repo id; use <user-or-org>/<name> "
            "with letters, digits, '-', '_' or '.'"
        )
    if repo_id.split("/", 1)[1].startswith("eval_"):
        # lerobot-record refuses these too: LeRobot reserves eval_ for policy evaluation runs.
        raise SettingsError(f"{variable}={repo_id!r}: names starting with eval_ are reserved by LeRobot")
    return repo_id


def is_local_repo(repo_id):
    return repo_id.split("/", 1)[0] == "local"


def push_decision(mode, repo_id, token_set):
    """Decide whether to upload the finished dataset. Returns (push, reason)."""
    if mode == "false":
        return False, "upload is off (SO101_PUSH_TO_HUB=false)"
    if is_local_repo(repo_id):
        if mode == "true":
            raise SettingsError(
                "SO101_PUSH_TO_HUB=true needs SO101_DATASET_REPO_ID=<your-hf-user>/<dataset>, not a local/ name"
            )
        return False, "local dataset only (set SO101_DATASET_REPO_ID and HF_TOKEN to upload)"
    if not token_set:
        if mode == "true":
            raise SettingsError("SO101_PUSH_TO_HUB=true needs HF_TOKEN (a write token for the dataset repo)")
        return False, "no HF_TOKEN, so the dataset stays on the device"
    return True, f"uploads to https://huggingface.co/datasets/{repo_id} when the session ends"


def dataset_state(root):
    """What is at the dataset directory: new, empty, resume, or foreign.

    - "new": nothing there yet; create the dataset.
    - "empty": a LeRobot dataset with no saved episode (an earlier start
      failed before the first one); it holds nothing, so it is recreated.
    - "resume": a LeRobot dataset with episodes; new episodes are appended.
    - "foreign": files that are not a LeRobot dataset; the app refuses to touch them.
    """
    root = Path(root)
    if not root.exists() or (root.is_dir() and not any(root.iterdir())):
        return "new"
    info_path = root / "meta" / "info.json"
    if not info_path.is_file():
        return "foreign"
    try:
        info = json.loads(info_path.read_text())
    except (OSError, ValueError):
        return "foreign"
    if not isinstance(info, dict):
        return "foreign"
    return "resume" if int(info.get("total_episodes") or 0) > 0 else "empty"


def load(env):
    """Build RecordSettings from an environment mapping; raises SettingsError."""
    repo_id = check_repo_id(env_str(env, "SO101_DATASET_REPO_ID") or DEFAULT_REPO_ID)
    mode = env_str(env, "SO101_PUSH_TO_HUB", "auto").lower() or "auto"
    if mode not in PUSH_MODES:
        raise SettingsError(f"SO101_PUSH_TO_HUB={mode!r} must be auto, true or false")
    push, reason = push_decision(mode, repo_id, bool(env_str(env, "HF_TOKEN")))

    cameras = parse_cameras(env_str(env, "SO101_CAMERAS", "front=/dev/video0"), default_name="front")

    codec = env_str(env, "SO101_VIDEO_CODEC", "libsvtav1")
    if codec not in VIDEO_CODECS:
        raise SettingsError(f"SO101_VIDEO_CODEC={codec!r} must be one of {', '.join(VIDEO_CODECS)}")

    task = env_str(env, "SO101_TASK", DEFAULT_TASK)
    if not task:
        raise SettingsError("SO101_TASK is empty; describe the task in one sentence")

    follower_port = serial_port(env, "SO101_FOLLOWER_PORT", "/dev/ttyACM0")
    leader_port = serial_port(env, "SO101_LEADER_PORT", "/dev/ttyACM1")
    if follower_port == leader_port:
        raise SettingsError(f"SO101_FOLLOWER_PORT and SO101_LEADER_PORT are both {follower_port}")

    follower_calibration = Path(env_str(env, "SO101_FOLLOWER_CALIBRATION", "/calibration/follower.json"))
    leader_calibration = Path(env_str(env, "SO101_LEADER_CALIBRATION", "/calibration/leader.json"))
    if follower_calibration == leader_calibration:
        raise SettingsError(
            f"SO101_FOLLOWER_CALIBRATION and SO101_LEADER_CALIBRATION are both {follower_calibration}; "
            "each arm needs its own calibration"
        )

    lerobot_home = Path(env_str(env, "HF_LEROBOT_HOME", "/data/lerobot"))
    return RecordSettings(
        follower_port=follower_port,
        leader_port=leader_port,
        follower_calibration=follower_calibration,
        leader_calibration=leader_calibration,
        cameras=cameras,
        camera_width=env_int(env, "SO101_CAMERA_WIDTH", 640, minimum=16, maximum=7680),
        camera_height=env_int(env, "SO101_CAMERA_HEIGHT", 480, minimum=16, maximum=4320),
        camera_fourcc=camera_fourcc(env),
        fps=env_int(env, "SO101_FPS", 30, minimum=1, maximum=120),
        task=task,
        num_episodes=env_int(env, "SO101_NUM_EPISODES", 50, minimum=1, maximum=100000),
        episode_seconds=env_float(env, "SO101_EPISODE_SECONDS", 30.0, minimum=1.0),
        reset_seconds=env_float(env, "SO101_RESET_SECONDS", 10.0, minimum=0.0),
        repo_id=repo_id,
        # LeRobot's own layout: $HF_LEROBOT_HOME/<repo_id>.
        dataset_root=lerobot_home / repo_id,
        push=push,
        push_reason=reason,
        private=env_bool(env, "SO101_DATASET_PRIVATE", True),
        use_degrees=env_bool(env, "SO101_USE_DEGREES", True),
        video_codec=codec,
        streaming_encoding=env_bool(env, "SO101_STREAMING_ENCODING", True),
        encoder_threads=env_int(env, "SO101_ENCODER_THREADS", 2, minimum=1, maximum=64),
        http_port=env_int(env, "SO101_HTTP_PORT", 8092, minimum=1, maximum=65535),
        dry_run=env_bool(env, "SO101_DRY_RUN", False),
    )
