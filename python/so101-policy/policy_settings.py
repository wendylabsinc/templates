"""Policy runner settings from environment variables (set with `wendy run --env`).

Standard library only, so the repository tests can check it without LeRobot.
The README lists every variable. This module also holds the pure decisions
the runner makes about the model: where it comes from, which commit to load,
and whether its inputs match the arm and cameras.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from so101_common import (
    DEFAULT_TASK,
    JOINTS,
    SettingsError,
    camera_fourcc,
    env_bool,
    env_float,
    env_int,
    env_str,
    first_line,
    parse_cameras,
    serial_port,
)

# A public ACT policy for the SO-101 follower: one 640x480 wrist camera, joints
# in degrees (LeRobot 0.6.1's default), trained with LeRobot's processor format.
DEFAULT_MODEL = "TakuyaHiraoka/act_so101_pick_diverse_objects"
# The policy types this image can run. Each needs its LeRobot extra in
# requirements.txt (smolvla); ACT needs none.
SUPPORTED_POLICIES = ("act", "smolvla")
DEVICES = ("auto", "cpu", "cuda")

REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
# A branch, a tag or a commit. Git allows more, but not spaces, "..", or a
# leading "-" or "/", and the Hub's names stay within these characters.
REVISION = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}$")


@dataclass(frozen=True)
class PolicySettings:
    model_path: str
    model_is_local: bool
    model_revision: str | None
    follower_port: str
    follower_calibration: Path
    cameras: dict
    camera_width: int | None
    camera_height: int | None
    camera_fourcc: str | None
    fps: int
    task: str
    device: str
    use_degrees: bool
    max_relative_target: float | None
    return_seconds: float
    run_seconds: float
    autostart: bool
    http_port: int
    dry_run: bool


# ── which model, and which commit of it ──────────────────────────────────────


def is_commit(revision):
    return bool(revision) and COMMIT.match(revision) is not None


def check_revision(revision, variable="SO101_MODEL_REVISION"):
    if not REVISION.match(revision) or ".." in revision or revision.endswith(("/", ".lock")):
        raise SettingsError(
            f"{variable}={revision!r} is not a branch, tag or commit; use a name such as main or v1, "
            "or the 40-character commit id from the model's Files and versions page"
        )
    return revision


def model_source(path, revision, is_dir=os.path.isdir):
    """Check SO101_MODEL_PATH and SO101_MODEL_REVISION. Returns True for a local directory.

    A path starting with /, ./ or ../, or an existing directory, is local; it
    has no Hub revision. Anything else must be a Hub repo id, <owner>/<name>.
    """
    if not path:
        raise SettingsError("SO101_MODEL_PATH is empty; set a Hugging Face model id or a directory")
    if path.startswith(("/", "./", "../")) or is_dir(path):
        if revision:
            raise SettingsError(
                f"SO101_MODEL_REVISION={revision!r} applies to Hub models only, but "
                f"SO101_MODEL_PATH={path} is a directory; unset SO101_MODEL_REVISION"
            )
        if not is_dir(path):
            raise SettingsError(f"SO101_MODEL_PATH={path} is not a directory in the container")
        return True
    if not REPO_ID.match(path) or ".." in path or "--" in path:
        raise SettingsError(
            f"SO101_MODEL_PATH={path!r} is neither a Hugging Face model id (<owner>/<name>) nor a directory"
        )
    if revision:
        check_revision(revision)
    return False


def resolve_revision(repo_id, requested, lookup, cached=None):
    """Turn the requested revision (or main) into the commit it points at now.

    `lookup(repo_id, revision)` asks the Hub and returns a commit id;
    `cached(repo_id, revision)` returns the commit of a copy already in the
    local Hugging Face cache, or None. Loading by commit keeps the config, the
    weights and the processors from one snapshot, even if the branch moves.

    Returns (commit, description) for the log and the status page.
    """
    label = requested or "main"
    try:
        commit = lookup(repo_id, requested)
    except Exception as exc:  # noqa: BLE001 - network, auth or not-found errors all land here
        reason = first_line(exc)
        if is_commit(requested):
            return requested, f"commit {requested} (not checked on the Hub: {reason})"
        local = cached(repo_id, label) if cached is not None else None
        if is_commit(local):
            return local, f"{label} -> commit {local} (from the local cache; the Hub did not answer: {reason})"
        hint = "check the id and revision; a private model needs HF_TOKEN with read access"
        raise SettingsError(f"could not resolve {repo_id}@{label} on the Hugging Face Hub ({reason}); {hint}")
    if not is_commit(commit):
        raise SettingsError(f"the Hub returned {commit!r} for {repo_id}@{label}, which is not a commit id")
    if is_commit(requested) and commit != requested:
        raise SettingsError(f"the Hub resolved commit {requested} of {repo_id} to {commit}")
    if label == commit:
        return commit, f"commit {commit}"
    return commit, f"{label} -> commit {commit}"


def commit_from_cache_path(path):
    """The commit in a Hugging Face cache path: .../snapshots/<commit>/<file>."""
    parts = Path(str(path)).parts
    if "snapshots" in parts:
        index = parts.index("snapshots")
        if index + 1 < len(parts) and is_commit(parts[index + 1]):
            return parts[index + 1]
    return None


# ── does the model fit this arm and these cameras? ───────────────────────────


def check_policy_type(policy_type, model):
    if policy_type not in SUPPORTED_POLICIES:
        raise SettingsError(
            f"{model} is a {policy_type!r} policy; this app runs {' and '.join(SUPPORTED_POLICIES)}. "
            "Another type needs its LeRobot extra in requirements.txt and an entry in SUPPORTED_POLICIES."
        )
    return policy_type


def check_model_features(inputs, outputs, model):
    """Check the policy's inputs and outputs against the SO-101 follower.

    `inputs` and `outputs` map feature names to (type, shape), as in the
    model's config.json: types are STATE, VISUAL, ACTION. Returns the camera
    names the model expects, with their (height, width).
    """
    state = inputs.get("observation.state")
    if state is None or tuple(state[1]) != (len(JOINTS),):
        raise SettingsError(
            f"{model} does not take a {len(JOINTS)}-joint observation.state (it takes {state}); "
            "it was not trained on an SO-101 arm"
        )
    action = outputs.get("action")
    if action is None or tuple(action[1]) != (len(JOINTS),):
        raise SettingsError(f"{model} does not output a {len(JOINTS)}-joint action (it outputs {action})")
    cameras = {}
    for name, (kind, shape) in inputs.items():
        if kind != "VISUAL":
            continue
        if not name.startswith("observation.images."):
            raise SettingsError(f"{model} has an image input {name!r} outside observation.images.*")
        if len(shape) != 3 or shape[0] != 3:
            raise SettingsError(f"{model} expects {name} with shape {shape}; this app captures color images")
        camera = name.removeprefix("observation.images.")
        if camera.startswith("empty_camera_"):
            continue  # SmolVLA's placeholder for a missing view; the model fills it in itself
        cameras[camera] = (int(shape[1]), int(shape[2]))
    unknown = [name for name, (kind, _shape) in inputs.items() if kind not in ("STATE", "VISUAL")]
    if unknown:
        raise SettingsError(f"{model} needs inputs this app does not provide: {', '.join(sorted(unknown))}")
    if not cameras:
        raise SettingsError(f"{model} takes no camera image; this app expects a vision policy")
    return cameras


def match_cameras(configured, model_cameras):
    """Map the SO101_CAMERAS entries to the model's camera names.

    `configured` is {name or None: device}. One unnamed device takes the name
    of a single-camera model. Returns {name: device}.
    """
    if None in configured:
        if len(configured) != 1 or len(model_cameras) != 1:
            names = ", ".join(sorted(model_cameras))
            raise SettingsError(
                f"SO101_CAMERAS: name every camera, because the model takes {len(model_cameras)} "
                f"({names}); for example {next(iter(sorted(model_cameras)))}=/dev/video0"
            )
        return {next(iter(model_cameras)): configured[None]}
    if set(configured) != set(model_cameras):
        raise SettingsError(
            f"SO101_CAMERAS names {', '.join(sorted(configured))}, but the model expects "
            f"{', '.join(sorted(model_cameras))}; give each device the model's camera name"
        )
    return dict(configured)


def choose_device(requested, cuda_available):
    """The torch device for SO101_POLICY_DEVICE: auto picks CUDA when torch can use it."""
    if requested == "auto":
        return "cuda" if cuda_available else "cpu"
    if requested == "cuda" and not cuda_available:
        raise SettingsError(
            "SO101_POLICY_DEVICE=cuda, but torch in this image sees no CUDA GPU; use auto or cpu "
            "(the README explains which devices get a CUDA build)"
        )
    return requested


def camera_size(settings, model_size):
    """(width, height) to capture: the model's training size unless set explicitly."""
    height, width = model_size
    return (settings.camera_width or width, settings.camera_height or height)


# ── the environment ──────────────────────────────────────────────────────────


def optional_float(env, name, default, minimum=None):
    raw = env_str(env, name, default)
    if raw.lower() in ("", "none", "off"):
        return None
    return env_float({name: raw}, name, None, minimum=minimum)


def load(env, is_dir=os.path.isdir):
    """Build PolicySettings from an environment mapping; raises SettingsError."""
    model_path = env_str(env, "SO101_MODEL_PATH", DEFAULT_MODEL)
    revision = env_str(env, "SO101_MODEL_REVISION") or None
    is_local = model_source(model_path, revision, is_dir=is_dir)

    device = env_str(env, "SO101_POLICY_DEVICE", "auto").lower() or "auto"
    if device not in DEVICES:
        raise SettingsError(f"SO101_POLICY_DEVICE={device!r} must be one of {', '.join(DEVICES)}")

    width = env_int(env, "SO101_CAMERA_WIDTH", 0, minimum=0, maximum=7680) or None
    height = env_int(env, "SO101_CAMERA_HEIGHT", 0, minimum=0, maximum=4320) or None
    if (width is None) != (height is None):
        raise SettingsError("set both SO101_CAMERA_WIDTH and SO101_CAMERA_HEIGHT, or neither")

    task = env_str(env, "SO101_TASK", DEFAULT_TASK)
    if not task:
        raise SettingsError("SO101_TASK is empty; use the task sentence the training data was recorded with")

    return PolicySettings(
        model_path=model_path,
        model_is_local=is_local,
        model_revision=revision,
        follower_port=serial_port(env, "SO101_FOLLOWER_PORT", "/dev/ttyACM0"),
        follower_calibration=Path(env_str(env, "SO101_FOLLOWER_CALIBRATION", "/calibration/follower.json")),
        cameras=parse_cameras(env_str(env, "SO101_CAMERAS", "/dev/video0"), default_name=None),
        camera_width=width,
        camera_height=height,
        camera_fourcc=camera_fourcc(env),
        fps=env_int(env, "SO101_FPS", 30, minimum=1, maximum=120),
        task=task,
        device=device,
        use_degrees=env_bool(env, "SO101_USE_DEGREES", True),
        max_relative_target=optional_float(env, "SO101_MAX_RELATIVE_TARGET", "10", minimum=0.1),
        return_seconds=env_float(env, "SO101_RETURN_SECONDS", 3.0, minimum=0.0, maximum=8.0),
        run_seconds=env_float(env, "SO101_RUN_SECONDS", 0.0, minimum=0.0),
        autostart=env_bool(env, "SO101_AUTOSTART", False),
        http_port=env_int(env, "SO101_HTTP_PORT", 8093, minimum=1, maximum=65535),
        dry_run=env_bool(env, "SO101_DRY_RUN", False),
    )
