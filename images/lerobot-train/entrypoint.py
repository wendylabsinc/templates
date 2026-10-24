#!/usr/bin/env python3
"""Entrypoint of the `wendy train` image.

It wraps LeRobot's own `lerobot-train` and adds what a rented, preemptible GPU
job needs:

1. It validates its inputs and resolves the dataset revision to a commit
   before any GPU time is spent.
2. It resumes from the newest complete checkpoint in WENDY_TRAIN_OUTPUT_DIR.
3. It prints machine-readable `WENDY_TRAIN {json}` lines on stdout, which the
   `wendy train` CLI parses. Everything else, including lerobot-train's own
   log, goes to stderr.
4. It pushes the final model in one commit, with a model card that names the
   dataset commit, the policy, the step count, the image and the LeRobot
   version, and reports that commit.

Inputs are environment variables only; README.md documents them and the
events. The secrets in HF_TOKEN (and WANDB_API_KEY, for teams that enable
wandb) are never printed: every line this program writes is scrubbed of them.
"""

from __future__ import annotations

import collections
import contextlib
import importlib.metadata
import json
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

EVENT_PREFIX = "WENDY_TRAIN "
DEFAULT_OUTPUT_DIR = "/workspace/output"
DEFAULT_IMAGE = "ghcr.io/wendylabsinc/lerobot-train:lerobot-0.6.1"

# lerobot-train's --output_dir, inside WENDY_TRAIN_OUTPUT_DIR. lerobot-train
# refuses to start a fresh run in an existing directory, and the output
# directory is often a mount point, so the run gets a subdirectory we own.
RUN_SUBDIR = "train"
# Layout written by lerobot.common.train_utils.save_checkpoint (LeRobot 0.6.1).
CHECKPOINTS_DIR = "checkpoints"
LAST_CHECKPOINT_LINK = "last"
PRETRAINED_MODEL_DIR = "pretrained_model"
TRAINING_STATE_DIR = "training_state"
TRAIN_CONFIG_FILE = "train_config.json"

# Complete checkpoints to keep on disk. A SmolVLA checkpoint is a few GB, and
# lerobot-train never deletes one.
KEEP_CHECKPOINTS = 2
# Emit progress at least every 1% of the steps or every this many seconds.
PROGRESS_INTERVAL_S = 30.0
# How often to look for a new checkpoint while lerobot-train runs.
CHECKPOINT_POLL_S = 2.0
# After SIGTERM, how long lerobot-train gets before it is killed.
TERMINATE_GRACE_S = 20.0
# After lerobot-train exits, how long its dataloader workers may hold the log
# pipe open.
DRAIN_GRACE_S = 15.0
# Pauses between attempts to push the model.
PUSH_RETRY_DELAYS_S = (10.0, 30.0)

SECRET_ENV_VARS = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "WANDB_API_KEY")

REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

# The SmolVLA weights every SmolVLA run starts from. `--policy.type=smolvla`
# alone would leave the vision-language backbone randomly initialized
# (SmolVLAConfig.load_vlm_weights defaults to False), so runs fine-tune the
# published base model, as LeRobot's SmolVLA guide does. Pinned to the commit
# that was `main` on 2026-10-22; its weights date from 2025-10-10.
SMOLVLA_BASE = "lerobot/smolvla_base"
SMOLVLA_BASE_REVISION = "5e8d12a6e2975b0e5e5fce7c8caf47c371d257b6"


@dataclass(frozen=True)
class PolicySpec:
    default_steps: int
    default_batch_size: int
    init_args: tuple[str, ...]
    module: str
    class_name: str
    base_model: str | None = None
    base_model_revision: str | None = None


POLICIES: dict[str, PolicySpec] = {
    # LeRobot's default ACT batch size (and the ACT paper's).
    "act": PolicySpec(
        default_steps=20_000,
        default_batch_size=8,
        init_args=("--policy.type=act",),
        module="lerobot.policies.act.modeling_act",
        class_name="ACTPolicy",
    ),
    # Batch size from LeRobot's SmolVLA fine-tuning guide.
    "smolvla": PolicySpec(
        default_steps=20_000,
        default_batch_size=64,
        init_args=(
            f"--policy.path={SMOLVLA_BASE}",
            f"--policy.pretrained_revision={SMOLVLA_BASE_REVISION}",
        ),
        module="lerobot.policies.smolvla.modeling_smolvla",
        class_name="SmolVLAPolicy",
        base_model=SMOLVLA_BASE,
        base_model_revision=SMOLVLA_BASE_REVISION,
    ),
}

# lerobot-train flags the entrypoint sets itself. WENDY_TRAIN_EXTRA_ARGS may
# not override them, because resume, the reported events and the push depend
# on them. The value names what to set instead.
OWNED_FLAGS: dict[str, str] = {
    "--dataset.repo_id": "set WENDY_TRAIN_DATASET instead",
    "--dataset.revision": "set WENDY_TRAIN_DATASET_REVISION instead",
    "--policy.type": "set WENDY_TRAIN_POLICY instead",
    "--policy.path": "set WENDY_TRAIN_POLICY instead",
    "--policy.pretrained_revision": "the image pins the base model",
    "--policy.push_to_hub": "the entrypoint pushes the model; set WENDY_TRAIN_PUSH instead",
    "--policy.repo_id": "set WENDY_TRAIN_MODEL_REPO instead",
    "--save_checkpoint_to_hub": "the entrypoint pushes the model; set WENDY_TRAIN_PUSH instead",
    "--steps": "set WENDY_TRAIN_STEPS instead",
    "--batch_size": "set WENDY_TRAIN_BATCH_SIZE instead",
    "--output_dir": "set WENDY_TRAIN_OUTPUT_DIR instead",
    "--resume": "runs resume automatically from WENDY_TRAIN_OUTPUT_DIR",
    "--config_path": "runs resume automatically from WENDY_TRAIN_OUTPUT_DIR",
    "--save_checkpoint": "checkpoints are needed to resume and to push",
    "--job.target": "this image trains locally on its own GPU",
    "--help": "it would print help instead of training",
    "-h": "it would print help instead of training",
}

# Lines lerobot-train writes (LeRobot 0.6.1, tqdm 4.70). tests/test_entrypoint.py
# holds lines captured from the real ones and checks these patterns against them.
# MetricsTracker.__str__: "step:5K smpl:37K ep:142 epch:2.83 loss:0.123 ..."
TRACKER_RE = re.compile(r"\bstep:(?P<step>\d+(?:\.\d+)?[KMBTQ]?) smpl:\S+ ep:\S+ epch:\S+")
LOSS_RE = re.compile(r"(?:^|\s)loss:(?P<loss>\S+)")
# lerobot_train.py, just before save_checkpoint().
CHECKPOINT_LOG_RE = re.compile(r"\bCheckpoint policy after step (?P<step>\d+)\b")
# The training progress bar: "Training:  26%|███▌      | 13/50 [00:00<00:00, 41.26step/s]"
TQDM_RE = re.compile(r"\bTraining:\s*\d+%\|[^|]*\|\s*(?P<n>\d+)/(?P<total>\d+)\b")
# The last line of a Python traceback ("RuntimeError: CUDA out of memory"),
# or the "error: ..." line LeRobot's config parser prints before exiting.
EXCEPTION_RE = re.compile(
    r"^(?:(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:Error|Exception|Interrupt|Exit)\b(?::.*)?|error: .+)$"
)


class TrainError(Exception):
    """A failure reported as an `error` event."""

    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


class InputError(TrainError):
    """Bad input. The message names the variable to fix."""

    def __init__(self, message: str):
        super().__init__(message, exit_code=2)


class Interrupted(TrainError):
    def __init__(self, signum: int):
        name = signal.Signals(signum).name
        super().__init__(
            f"stopped by {name} (the job was cancelled or preempted); a restarted job "
            "resumes from the newest checkpoint in WENDY_TRAIN_OUTPUT_DIR",
            exit_code=128 + signum,
        )


# ---------------------------------------------------------------------------
# Output


class Redactor:
    """Replaces secret values from the environment with `***`."""

    def __init__(self, env: Mapping[str, str]):
        values = {env.get(name, "").strip() for name in SECRET_ENV_VARS}
        # Very short values would mangle ordinary text and are not real tokens.
        self._secrets = sorted((v for v in values if len(v) >= 8), key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


class Output:
    """Events on stdout, human-readable notes and lerobot-train's log on stderr."""

    def __init__(self, redact: Redactor, stdout: TextIO, stderr: TextIO):
        self.redact = redact
        self._stdout = stdout
        self._stderr = stderr

    def event(self, event: str, **fields: object) -> None:
        payload: dict[str, object] = {"event": event}
        for key, value in fields.items():
            payload[key] = self.redact(value) if isinstance(value, str) else value
        line = EVENT_PREFIX + json.dumps(payload, separators=(",", ":"), allow_nan=False)
        self._stdout.write(self.redact(line) + "\n")
        self._stdout.flush()

    def note(self, message: str) -> None:
        self._stderr.write(self.redact(f"[wendy-train] {message}") + "\n")
        self._stderr.flush()

    def forward(self, line: str) -> None:
        # Keep a child line from ever looking like one of our events.
        if line.startswith(EVENT_PREFIX.strip()):
            line = "| " + line
        self._stderr.write(self.redact(line) + "\n")
        self._stderr.flush()


# ---------------------------------------------------------------------------
# Inputs


@dataclass(frozen=True)
class Config:
    dataset: str
    dataset_revision: str
    policy: str
    steps: int
    batch_size: int
    push: bool
    model_repo: str
    output_dir: Path
    extra_args: tuple[str, ...]
    token: str | None

    @property
    def spec(self) -> PolicySpec:
        return POLICIES[self.policy]

    @property
    def run_dir(self) -> Path:
        return self.output_dir / RUN_SUBDIR


def parse_config(env: Mapping[str, str]) -> Config:
    """Reads and validates every input, reporting all problems at once."""
    problems: list[str] = []

    def value(name: str) -> str:
        return env.get(name, "").strip()

    dataset = value("WENDY_TRAIN_DATASET")
    if not dataset:
        problems.append("WENDY_TRAIN_DATASET is required: a Hugging Face dataset repo id such as user/so101_pick")
    elif not is_repo_id(dataset):
        problems.append(f"WENDY_TRAIN_DATASET={dataset!r} is not a Hugging Face repo id of the form namespace/name")

    revision = value("WENDY_TRAIN_DATASET_REVISION") or "main"
    if not REVISION_RE.match(revision) or ".." in revision:
        problems.append(f"WENDY_TRAIN_DATASET_REVISION={revision!r} is not a branch, tag or commit")

    raw_policy = value("WENDY_TRAIN_POLICY")
    policy = raw_policy.lower()
    spec = POLICIES.get(policy)
    if not raw_policy:
        problems.append("WENDY_TRAIN_POLICY is required: act or smolvla")
    elif spec is None:
        problems.append(f"WENDY_TRAIN_POLICY={raw_policy!r} is not supported: use act or smolvla")

    steps = parse_positive_int(env, "WENDY_TRAIN_STEPS", spec.default_steps if spec else 1, 10_000_000, problems)
    batch_size = parse_positive_int(
        env, "WENDY_TRAIN_BATCH_SIZE", spec.default_batch_size if spec else 1, 65_536, problems
    )

    push = True
    raw_push = value("WENDY_TRAIN_PUSH")
    if raw_push:
        lowered = raw_push.lower()
        if lowered in ("true", "1", "yes"):
            push = True
        elif lowered in ("false", "0", "no"):
            push = False
        else:
            problems.append(f"WENDY_TRAIN_PUSH={raw_push!r} must be true or false")

    model_repo = value("WENDY_TRAIN_MODEL_REPO")
    if model_repo and not is_repo_id(model_repo):
        problems.append(
            f"WENDY_TRAIN_MODEL_REPO={model_repo!r} is not a Hugging Face repo id of the form namespace/name"
        )
    elif push and not model_repo:
        problems.append("WENDY_TRAIN_MODEL_REPO is required when WENDY_TRAIN_PUSH=true")

    token = env.get("HF_TOKEN", "").strip() or None
    if push and token is None:
        problems.append(
            "HF_TOKEN is required when WENDY_TRAIN_PUSH=true: a token that can write WENDY_TRAIN_MODEL_REPO"
        )

    output_dir = Path(value("WENDY_TRAIN_OUTPUT_DIR") or DEFAULT_OUTPUT_DIR).expanduser().absolute()

    extra_args = parse_extra_args(env.get("WENDY_TRAIN_EXTRA_ARGS", ""), problems)

    if problems:
        raise InputError("; ".join(problems))
    return Config(
        dataset=dataset,
        dataset_revision=revision,
        policy=policy,
        steps=steps,
        batch_size=batch_size,
        push=push,
        model_repo=model_repo if push else "",
        output_dir=output_dir,
        extra_args=extra_args,
        token=token,
    )


def is_repo_id(value: str) -> bool:
    return bool(REPO_ID_RE.match(value)) and ".." not in value and "--" not in value


def parse_positive_int(env: Mapping[str, str], name: str, default: int, maximum: int, problems: list[str]) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        number = int(raw, 10)
    except ValueError:
        problems.append(f"{name}={raw!r} is not a whole number")
        return default
    if not 1 <= number <= maximum:
        problems.append(f"{name}={raw!r} must be between 1 and {maximum}")
        return default
    return number


def parse_extra_args(raw: str, problems: list[str]) -> tuple[str, ...]:
    """WENDY_TRAIN_EXTRA_ARGS: a JSON array of strings, passed through verbatim."""
    if not raw.strip():
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        problems.append(f"WENDY_TRAIN_EXTRA_ARGS is not valid JSON ({exc.msg}); expected an array of strings")
        return ()
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        problems.append('WENDY_TRAIN_EXTRA_ARGS must be a JSON array of strings, e.g. ["--num_workers=8"]')
        return ()
    for item in parsed:
        if "\x00" in item:
            problems.append("WENDY_TRAIN_EXTRA_ARGS contains a NUL character")
            return ()
        flag = item.split("=", 1)[0]
        if flag in OWNED_FLAGS:
            problems.append(f"WENDY_TRAIN_EXTRA_ARGS may not set {flag}: {OWNED_FLAGS[flag]}")
    return tuple(parsed)


def last_int_flag(args: list[str] | tuple[str, ...], name: str) -> int | None:
    """The value lerobot-train ends up with for `--name` (the last one wins)."""
    flag = f"--{name}"
    found: str | None = None
    for index, arg in enumerate(args):
        if arg.startswith(flag + "="):
            found = arg.split("=", 1)[1]
        elif arg == flag and index + 1 < len(args):
            found = args[index + 1]
    try:
        return int(found) if found is not None else None
    except ValueError:
        return None


def default_log_freq(steps: int) -> int:
    """A log line at least every 1% of the steps (LeRobot's default is 200)."""
    return max(1, min(200, steps // 100))


def default_save_freq(steps: int) -> int:
    """A checkpoint every 10% of the steps, no more often than every 1000.

    lerobot-train always saves after the last step as well. A preempted run
    loses at most the steps since the newest checkpoint.
    """
    return max(1000, math.ceil(steps / 10))


# ---------------------------------------------------------------------------
# Checkpoints


@dataclass(frozen=True)
class Checkpoint:
    step: int
    path: Path

    @property
    def pretrained_dir(self) -> Path:
        return self.path / PRETRAINED_MODEL_DIR

    @property
    def train_config(self) -> Path:
        return self.pretrained_dir / TRAIN_CONFIG_FILE


def checkpoint_dirs(run_dir: Path) -> list[tuple[int, Path]]:
    root = run_dir / CHECKPOINTS_DIR
    if not root.is_dir():
        return []
    found = []
    for entry in root.iterdir():
        if entry.name.isdigit() and entry.is_dir() and not entry.is_symlink():
            found.append((int(entry.name), entry))
    return sorted(found)


def last_link(run_dir: Path) -> Path | None:
    """Where `checkpoints/last` points. lerobot-train moves it only after a save completes."""
    link = run_dir / CHECKPOINTS_DIR / LAST_CHECKPOINT_LINK
    try:
        target = os.readlink(link)
    except OSError:
        return None
    path = link.parent / target
    return path if path.name.isdigit() else None


def is_complete(path: Path, step: int) -> bool:
    pretrained = path / PRETRAINED_MODEL_DIR
    state = path / TRAINING_STATE_DIR
    required = [
        pretrained / "config.json",
        pretrained / TRAIN_CONFIG_FILE,
        state / "training_step.json",
        state / "rng_state.safetensors",
    ]
    weights = [pretrained / "model.safetensors", pretrained / "adapter_model.safetensors"]
    if not all(f.is_file() and f.stat().st_size > 0 for f in required):
        return False
    if not any(f.is_file() and f.stat().st_size > 0 for f in weights):
        return False
    try:
        saved_step = json.loads((state / "training_step.json").read_text())["step"]
        json.loads((pretrained / TRAIN_CONFIG_FILE).read_text())
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return isinstance(saved_step, int) and not isinstance(saved_step, bool) and saved_step == step


def usable_checkpoints(run_dir: Path) -> list[Checkpoint]:
    """Complete checkpoints, oldest first.

    A step directory newer than `last` may have been half-written when the job
    was stopped, so it is skipped. Without `last` (the job stopped between
    lerobot-train removing and re-creating the link), every complete-looking
    directory counts.
    """
    last = last_link(run_dir)
    limit = int(last.name) if last is not None else None
    usable = []
    for step, path in checkpoint_dirs(run_dir):
        if limit is not None and step > limit:
            continue
        if is_complete(path, step):
            usable.append(Checkpoint(step, path))
    return usable


def step_identifier(step: int, total_steps: int) -> str:
    """lerobot.common.train_utils.get_step_identifier: the checkpoint directory name."""
    return f"{step:0{max(6, len(str(total_steps)))}d}"


def read_json(path: Path) -> dict:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return data


# ---------------------------------------------------------------------------
# lerobot-train log parsing


def format_big_number(num: float, precision: int = 0) -> str:
    """lerobot.utils.utils.format_big_number (LeRobot 0.6.1, Apache-2.0)."""
    suffixes = ["", "K", "M", "B", "T", "Q"]
    divisor = 1000.0
    for suffix in suffixes:
        if abs(num) < divisor:
            return f"{num:.{precision}f}{suffix}"
        num /= divisor
    return str(num)


def big_number_value(text: str) -> float:
    """The number format_big_number() printed as `text` stands for ("5K" -> 5000)."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMBTQ]?)", text)
    if not match:
        raise ValueError(text)
    number, suffix = float(match.group(1)), match.group(2)
    return number * 1000.0 ** ("KMBTQ".index(suffix) + 1) if suffix else number


def tracker_step(display: str, previous: int, log_freq: int | None) -> int:
    """The exact step of a MetricsTracker line.

    LeRobot rounds the step it prints ("step:5K" for 4600), but it logs exactly
    when `step % log_freq == 0`, so the line is the next multiple of log_freq
    after the previous one whose rounded form matches.
    """
    try:
        shown = big_number_value(display)
    except ValueError:
        return previous
    if log_freq and log_freq > 0:
        candidate = (previous // log_freq + 1) * log_freq
        while True:
            text = format_big_number(candidate)
            if text == display:
                return candidate
            if big_number_value(text) > shown:
                break
            candidate += log_freq
    return max(previous, int(shown))


def parse_loss(line: str) -> tuple[bool, float | None]:
    """Whether the line reports a loss, and its value when it is a finite number."""
    match = LOSS_RE.search(line)
    if not match:
        return False, None
    try:
        loss = float(match.group("loss"))
    except ValueError:
        return True, None
    return True, loss if math.isfinite(loss) else None


class TrainingMonitor:
    """Turns lerobot-train's log and checkpoint directory into events.

    Checkpoints are reported in order. lerobot-train logs "Checkpoint policy
    after step N" and then saves; the save is complete once `checkpoints/last`
    has reached N. If that log line ever changes, a new `last` target is still
    picked up when the directory is polled.
    """

    def __init__(
        self,
        out: Output,
        run_dir: Path,
        total: int,
        resumed_from: int,
        log_freq: int | None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._out = out
        self._run_dir = run_dir
        self._total = total
        self._resumed_from = resumed_from
        self._log_freq = log_freq
        self._clock = clock
        self._one_percent = max(1, total // 100)
        self.step = resumed_from
        self._last_logged = resumed_from
        self._loss: float | None = None
        self._emitted_step = resumed_from
        self._emitted_at = clock()
        self._polled_at = float("-inf")
        self._pending: set[int] = set()
        self._saw_checkpoint_line = False
        self.reported: list[int] = []

    def on_line(self, line: str) -> None:
        # Any line after "Checkpoint policy after step N" comes after its save.
        if self._pending:
            self.poll(force=True)
        tqdm = TQDM_RE.search(line)
        if tqdm and int(tqdm.group("total")) == self._total - self._resumed_from:
            self._advance(self._resumed_from + int(tqdm.group("n")))
        tracker = TRACKER_RE.search(line)
        if tracker:
            step = tracker_step(tracker.group("step"), self._last_logged, self._log_freq)
            self._last_logged = step
            found, loss = parse_loss(line[tracker.start() :])
            if found:
                # A NaN or infinite loss is left out rather than replaced by an older one.
                self._loss = loss
            self._advance(step)
        checkpoint = CHECKPOINT_LOG_RE.search(line)
        if checkpoint:
            step = int(checkpoint.group("step"))
            self._saw_checkpoint_line = True
            self._advance(step)
            if step > self._reported_step:
                self._pending.add(step)
        self.maybe_emit()

    @property
    def _reported_step(self) -> int:
        return self.reported[-1] if self.reported else self._resumed_from

    def _advance(self, step: int) -> None:
        self.step = max(self.step, min(step, self._total))

    def maybe_emit(self, force: bool = False) -> None:
        if self.step <= self._emitted_step:
            return
        now = self._clock()
        # Once per 1% of the steps (the first step known past each boundary),
        # and every PROGRESS_INTERVAL_S when steps are slow.
        due = (
            self.step // self._one_percent > self._emitted_step // self._one_percent
            or now - self._emitted_at >= PROGRESS_INTERVAL_S
            or self.step >= self._total
        )
        if not (due or force):
            return
        fields: dict[str, object] = {"step": self.step, "total": self._total}
        if self._loss is not None:
            fields["loss"] = self._loss
        self._out.event("progress", **fields)
        self._emitted_step = self.step
        self._emitted_at = now

    def poll(self, force: bool = False) -> None:
        now = self._clock()
        if not force and now - self._polled_at < CHECKPOINT_POLL_S:
            return
        self._polled_at = now
        last = last_link(self._run_dir)
        if last is not None:
            saved_up_to = int(last.name)
            candidates = set(self._pending)
            if not self._saw_checkpoint_line and saved_up_to > self._reported_step:
                candidates.add(saved_up_to)
            for step in sorted(candidates):
                if step > saved_up_to:
                    break
                self._pending.discard(step)
                path = self._run_dir / CHECKPOINTS_DIR / step_identifier(step, self._total)
                if step <= self._reported_step or not is_complete(path, step):
                    continue
                # Progress reaches a checkpoint's step before the checkpoint is reported.
                self._advance(step)
                self.maybe_emit(force=True)
                self._out.event("checkpoint", step=step)
                self.reported.append(step)
                self._prune()
        self.maybe_emit()

    def _prune(self) -> None:
        """Deletes checkpoints older than the newest KEEP_CHECKPOINTS reported ones."""
        if len(self.reported) < KEEP_CHECKPOINTS:
            return
        oldest_kept = self.reported[-KEEP_CHECKPOINTS]
        for step, path in checkpoint_dirs(self._run_dir):
            if step < oldest_kept:
                shutil.rmtree(path, ignore_errors=True)
                self._out.note(f"removed checkpoint {path.name}; keeping the newest {KEEP_CHECKPOINTS}")

    def finish(self, final_step: int) -> None:
        self._advance(final_step)
        self.maybe_emit(force=True)
        self.poll(force=True)


# ---------------------------------------------------------------------------
# Running lerobot-train


class Signals:
    """Forwards SIGTERM/SIGINT to lerobot-train, or stops the run before it starts."""

    def __init__(self) -> None:
        self.signum: int | None = None
        self.received_at = 0.0
        self.proc: subprocess.Popen | None = None

    def install(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, self._handle)

    def _handle(self, signum: int, _frame: object) -> None:
        first = self.signum is None
        self.signum = signum
        if first:
            self.received_at = time.monotonic()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            kill_group(proc, signum)
            return
        raise Interrupted(signum)


def kill_group(proc: subprocess.Popen, signum: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signum)


def read_lines(fd: int, lines: queue.Queue) -> None:
    """Splits the child's output on both \\n and \\r (tqdm redraws with \\r)."""
    pending = b""
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            pending += chunk
            parts = re.split(rb"[\r\n]", pending)
            pending = parts.pop()
            for part in parts:
                if part.strip():
                    lines.put(part.decode("utf-8", "replace"))
            if len(pending) > 1 << 20:
                lines.put(pending.decode("utf-8", "replace"))
                pending = b""
    except OSError:
        pass
    finally:
        if pending.strip():
            lines.put(pending.decode("utf-8", "replace"))
        lines.put(None)


def build_train_args(cfg: Config, dataset_sha: str, resume: Checkpoint | None) -> list[str]:
    if resume is not None:
        args = [f"--config_path={resume.train_config}", "--resume=true"]
    else:
        args = [f"--dataset.repo_id={cfg.dataset}", *cfg.spec.init_args]
    args += [
        f"--dataset.revision={dataset_sha}",
        f"--output_dir={cfg.run_dir}",
        f"--steps={cfg.steps}",
        f"--batch_size={cfg.batch_size}",
        f"--log_freq={default_log_freq(cfg.steps)}",
        f"--save_freq={default_save_freq(cfg.steps)}",
        "--save_checkpoint=true",
        # lerobot-train would push in three commits; the entrypoint pushes once.
        "--policy.push_to_hub=false",
    ]
    if cfg.model_repo:
        args.append(f"--policy.repo_id={cfg.model_repo}")
    args += cfg.extra_args
    return args


def run_lerobot_train(
    cfg: Config,
    lerobot_train: str,
    args: list[str],
    env: Mapping[str, str],
    resumed_from: int,
    out: Output,
    signals: Signals,
) -> Checkpoint:
    """Runs lerobot-train to the end and returns the final checkpoint."""
    child_env = dict(env)
    child_env.setdefault("PYTHONUNBUFFERED", "1")
    # tqdm redraws its bar ten times a second; in a job log that is noise.
    child_env.setdefault("TQDM_MININTERVAL", "10")
    child_env.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    child_env.setdefault("HF_DATASETS_DISABLE_PROGRESS_BARS", "1")

    monitor = TrainingMonitor(out, cfg.run_dir, cfg.steps, resumed_from, last_int_flag(args, "log_freq"))
    tail: collections.deque[str] = collections.deque(maxlen=200)
    lines: queue.Queue = queue.Queue()

    out.note("starting: lerobot-train " + " ".join(args))
    proc = subprocess.Popen(
        [lerobot_train, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=child_env,
        start_new_session=True,
    )
    signals.proc = proc
    assert proc.stdout is not None
    reader = threading.Thread(target=read_lines, args=(proc.stdout.fileno(), lines), daemon=True)
    reader.start()
    exited_at: float | None = None
    killed = False
    try:
        while True:
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                line = ""
            if line is None:
                break
            if line:
                out.forward(line)
                tail.append(line)
                monitor.on_line(line)
            monitor.poll()
            now = time.monotonic()
            if signals.signum is not None and not killed and now - signals.received_at > TERMINATE_GRACE_S:
                kill_group(proc, signal.SIGKILL)
                killed = True
            if exited_at is None and proc.poll() is not None:
                exited_at = now
            if exited_at is not None and now - exited_at > DRAIN_GRACE_S:
                # Leftover dataloader workers still hold the pipe open.
                kill_group(proc, signal.SIGKILL)
                break
        returncode = proc.wait()
    finally:
        signals.proc = None
        if proc.poll() is None:
            kill_group(proc, signal.SIGKILL)
            proc.wait()
        else:
            kill_group(proc, signal.SIGKILL)  # stray workers in the group
        # Close the pipe only once the reader is done with it: a closed fd
        # number can be reused at once, e.g. by a file the push is reading.
        reader.join(timeout=5)
        if not reader.is_alive():
            proc.stdout.close()

    if signals.signum is not None:
        raise Interrupted(signals.signum)
    if returncode != 0:
        if returncode < 0:
            reason = f"was killed by {signal.Signals(-returncode).name}"
            if returncode == -signal.SIGKILL:
                reason += " (out of memory?)"
            exit_code = 128 - returncode
        else:
            reason = f"exited with status {returncode}"
            exit_code = returncode
        cause = next((line.strip() for line in reversed(tail) if EXCEPTION_RE.match(line.strip())), "")
        message = f"lerobot-train {reason}"
        if cause:
            message += f": {cause[:500]}"
        raise TrainError(message, exit_code=exit_code)

    final = next((c for c in usable_checkpoints(cfg.run_dir) if c.step == cfg.steps), None)
    if final is None:
        raise TrainError(
            f"lerobot-train finished but wrote no complete checkpoint for step {cfg.steps} in {cfg.run_dir}"
        )
    monitor.finish(final.step)
    return final


# ---------------------------------------------------------------------------
# Hugging Face Hub


class Hub:
    """The Hub calls the entrypoint makes (huggingface_hub, as pinned in uv.lock)."""

    def __init__(self, token: str | None):
        from huggingface_hub import HfApi

        self._api = HfApi(token=token)
        self._token = token

    def resolve_dataset(self, repo_id: str, revision: str) -> str:
        return self._api.dataset_info(repo_id, revision=revision, token=self._token).sha

    def dataset_info_json(self, repo_id: str, revision: str) -> dict:
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(repo_id, "meta/info.json", repo_type="dataset", revision=revision, token=self._token)
        return read_json(Path(path))

    def ensure_model_repo(self, repo_id: str) -> None:
        self._api.create_repo(repo_id, repo_type="model", exist_ok=True, token=self._token)

    def push_model(self, repo_id: str, folder: Path, card: str, message: str) -> str:
        from huggingface_hub import CommitOperationAdd

        operations = [
            CommitOperationAdd(path_in_repo=path.relative_to(folder).as_posix(), path_or_fileobj=str(path))
            for path in sorted(folder.rglob("*"))
            if path.is_file() and path.relative_to(folder).as_posix() != "README.md"
        ]
        operations.append(CommitOperationAdd(path_in_repo="README.md", path_or_fileobj=card.encode("utf-8")))
        info = self._api.create_commit(
            repo_id, operations, commit_message=message, repo_type="model", token=self._token
        )
        return info.oid


def http_status(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def describe_dataset_error(exc: Exception, cfg: Config, revision: str) -> TrainError:
    from huggingface_hub import errors

    access = "HF_TOKEN is not set" if cfg.token is None else "HF_TOKEN cannot read it"
    if isinstance(exc, errors.GatedRepoError):
        return InputError(
            f"WENDY_TRAIN_DATASET: {cfg.dataset} is gated; accept its access conditions on the Hub "
            "with the account HF_TOKEN belongs to"
        )
    if isinstance(exc, errors.RepositoryNotFoundError):
        return InputError(f"WENDY_TRAIN_DATASET: dataset {cfg.dataset} was not found, or it is private and {access}")
    if isinstance(exc, errors.RevisionNotFoundError):
        return InputError(
            f"WENDY_TRAIN_DATASET_REVISION: revision {revision!r} does not exist in dataset {cfg.dataset}"
        )
    if http_status(exc) == 401:
        return InputError("HF_TOKEN was rejected by the Hugging Face Hub (401 Unauthorized)")
    return TrainError(f"could not read WENDY_TRAIN_DATASET={cfg.dataset} at {revision}: {exc}")


def resolve_dataset(hub: Hub, cfg: Config, resume_sha: str | None, out: Output) -> tuple[str, dict]:
    """The dataset commit to train on, and the dataset's meta/info.json at that commit."""
    try:
        sha = hub.resolve_dataset(cfg.dataset, cfg.dataset_revision)
    except Exception as exc:
        raise describe_dataset_error(exc, cfg, cfg.dataset_revision) from exc
    if not COMMIT_RE.match(sha or ""):
        raise TrainError(f"the Hub returned {sha!r} as the commit of {cfg.dataset}@{cfg.dataset_revision}")
    if resume_sha and resume_sha != sha:
        out.note(
            f"{cfg.dataset}@{cfg.dataset_revision} is now {sha}, but the checkpoint was trained on "
            f"{resume_sha}; the resumed run keeps training on {resume_sha}"
        )
        sha = resume_sha

    from huggingface_hub import errors

    try:
        info = hub.dataset_info_json(cfg.dataset, sha)
    except errors.EntryNotFoundError as exc:
        raise InputError(
            f"WENDY_TRAIN_DATASET: {cfg.dataset} has no meta/info.json at {sha}, so it is not a LeRobot dataset"
        ) from exc
    except Exception as exc:
        raise describe_dataset_error(exc, cfg, sha) from exc
    version = str(info.get("codebase_version", ""))
    if not version.startswith("v3."):
        raise InputError(
            f"WENDY_TRAIN_DATASET: {cfg.dataset} is a LeRobotDataset {version or 'of unknown version'}, but "
            f"LeRobot {lerobot_version()} trains v3.x datasets; convert it with LeRobot's "
            "convert_dataset_v21_to_v30 script first"
        )
    return sha, info


def ensure_model_repo(hub: Hub, cfg: Config) -> None:
    try:
        hub.ensure_model_repo(cfg.model_repo)
    except Exception as exc:
        status = http_status(exc)
        if status is None or status >= 500:
            raise TrainError(
                f"could not reach the Hub to create WENDY_TRAIN_MODEL_REPO={cfg.model_repo}: {exc}"
            ) from exc
        raise InputError(
            f"WENDY_TRAIN_MODEL_REPO: cannot create or write {cfg.model_repo} with HF_TOKEN ({status}): {exc}"
        ) from exc


def push_with_retries(hub: Hub, cfg: Config, folder: Path, card: str, message: str, out: Output) -> str:
    attempts = len(PUSH_RETRY_DELAYS_S) + 1
    for attempt in range(1, attempts + 1):
        try:
            revision = hub.push_model(cfg.model_repo, folder, card, message)
        except Exception as exc:
            status = http_status(exc)
            retryable = status is None or status == 429 or status >= 500
            if attempt == attempts or not retryable:
                raise TrainError(f"pushing to WENDY_TRAIN_MODEL_REPO={cfg.model_repo} failed: {exc}") from exc
            delay = PUSH_RETRY_DELAYS_S[attempt - 1]
            out.note(f"push attempt {attempt} failed ({exc}); retrying in {delay:.0f}s")
            time.sleep(delay)
            continue
        if not COMMIT_RE.match(revision or ""):
            raise TrainError(f"the Hub returned {revision!r} as the commit of the pushed model")
        return revision
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Model card


def lerobot_version() -> str:
    try:
        return importlib.metadata.version("lerobot")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def build_model_card(
    cfg: Config,
    dataset_sha: str,
    dataset_info: Mapping[str, object],
    steps: int,
    image: str,
    image_revision: str,
    version: str,
) -> str:
    spec = cfg.spec
    q = json.dumps  # a JSON string is a valid YAML scalar
    front = [
        "---",
        "library_name: lerobot",
        "pipeline_tag: robotics",
        "license: apache-2.0",
        "tags:",
        *(f"- {q(tag)}" for tag in ("robotics", "lerobot", cfg.policy, "wendy")),
        "datasets:",
        f"- {q(cfg.dataset)}",
    ]
    if spec.base_model:
        front.append(f"base_model: {q(spec.base_model)}")
    front.append("---")

    dataset_link = f"[`{cfg.dataset}`](https://huggingface.co/datasets/{cfg.dataset})"
    details = []
    for key, label in (("robot_type", "robot"), ("total_episodes", "episodes"), ("total_frames", "frames")):
        if dataset_info.get(key) not in (None, ""):
            details.append(f"{label} {dataset_info[key]}")
    if dataset_info.get("fps"):
        details.append(f"{dataset_info['fps']} fps")
    dataset_format = f"LeRobotDataset `{dataset_info.get('codebase_version', 'unknown')}`"
    if details:
        dataset_format += " (" + ", ".join(str(d) for d in details) + ")"
    image_cell = f"`{image}`"
    if image_revision and image_revision != "unknown":
        image_cell += f" built from wendylabsinc/templates commit `{image_revision}`"

    rows = [
        ("Policy", f"`{cfg.policy}`"),
        ("Dataset", f"{dataset_link} at commit `{dataset_sha}`"),
        ("Dataset format", dataset_format),
    ]
    if spec.base_model:
        base_link = f"[`{spec.base_model}`](https://huggingface.co/{spec.base_model})"
        rows.append(("Base model", f"{base_link} at commit `{spec.base_model_revision}`"))
    rows += [
        ("Training steps", str(steps)),
        ("Batch size", str(cfg.batch_size)),
        ("Training image", image_cell),
        ("LeRobot version", f"`{version}`"),
    ]
    table = ["| | |", "|---|---|", *(f"| {label} | {cell} |" for label, cell in rows)]

    body = [
        f"# {cfg.policy} policy trained on {cfg.dataset}",
        "",
        f"Trained by `wendy train` with [LeRobot](https://github.com/huggingface/lerobot) {version}.",
        "",
        *table,
    ]
    if cfg.extra_args:
        body += ["", "Extra `lerobot-train` arguments:", "", "```text", *cfg.extra_args, "```"]
    body += [
        "",
        "## Load it",
        "",
        "Pin the commit that `wendy train` reported, so a later push can't change what the robot runs:",
        "",
        "```python",
        f"from {spec.module} import {spec.class_name}",
        "",
        f'policy = {spec.class_name}.from_pretrained("{cfg.model_repo}", revision="<commit>")',
        "```",
        "",
        "`train_config.json` holds the complete LeRobot training configuration.",
        "",
    ]
    return "\n".join(front + body)


# ---------------------------------------------------------------------------
# Main


def prepare_run_dir(cfg: Config, out: Output) -> Checkpoint | None:
    """The checkpoint to resume from; clears a run directory that has none."""
    try:
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InputError(f"WENDY_TRAIN_OUTPUT_DIR={cfg.output_dir} cannot be created: {exc.strerror or exc}") from exc
    if not os.access(cfg.output_dir, os.W_OK | os.X_OK):
        raise InputError(f"WENDY_TRAIN_OUTPUT_DIR={cfg.output_dir} is not writable")

    usable = usable_checkpoints(cfg.run_dir)
    if usable:
        return usable[-1]
    if cfg.run_dir.exists():
        # Left by a run stopped before its first complete checkpoint;
        # lerobot-train will not start a fresh run over it.
        out.note(f"no complete checkpoint in {cfg.run_dir}; starting over")
        shutil.rmtree(cfg.run_dir)
    return None


def check_resume(cfg: Config, resume: Checkpoint) -> str | None:
    """Checks that the checkpoint belongs to this job; returns its dataset commit."""
    try:
        saved = read_json(resume.train_config)
        dataset = saved.get("dataset") or {}
        policy = saved.get("policy") or {}
    except (OSError, ValueError) as exc:
        raise TrainError(f"cannot read {resume.train_config}: {exc}") from exc
    if dataset.get("repo_id") != cfg.dataset or policy.get("type") != cfg.policy:
        raise InputError(
            f"WENDY_TRAIN_OUTPUT_DIR={cfg.output_dir} holds a checkpoint of a different job "
            f"({policy.get('type')} on {dataset.get('repo_id')}); use an empty directory for "
            f"{cfg.policy} on {cfg.dataset}"
        )
    if resume.step > cfg.steps:
        raise InputError(
            f"WENDY_TRAIN_STEPS={cfg.steps} is below step {resume.step} of the newest checkpoint "
            "in WENDY_TRAIN_OUTPUT_DIR"
        )
    revision = dataset.get("revision")
    return revision if isinstance(revision, str) and COMMIT_RE.match(revision) else None


def train(
    cfg: Config,
    env: Mapping[str, str],
    out: Output,
    signals: Signals,
    hub_factory: Callable[[str | None], Hub],
) -> None:
    lerobot_train = shutil.which("lerobot-train", path=env.get("PATH"))
    if lerobot_train is None:
        raise TrainError("lerobot-train is not on PATH")

    resume = prepare_run_dir(cfg, out)
    resume_sha = check_resume(cfg, resume) if resume else None

    hub = hub_factory(cfg.token)
    dataset_sha, dataset_info = resolve_dataset(hub, cfg, resume_sha, out)
    if cfg.push:
        ensure_model_repo(hub, cfg)

    version = lerobot_version()
    resumed_from = resume.step if resume else 0
    out.event(
        "start",
        dataset=cfg.dataset,
        dataset_revision=dataset_sha,
        policy=cfg.policy,
        steps=cfg.steps,
        lerobot_version=version,
        resumed_from_step=resumed_from,
    )

    if resume is not None and resume.step == cfg.steps:
        out.note(f"checkpoint {resume.path.name} already holds all {cfg.steps} steps; skipping training")
        final = resume
    else:
        if resume is not None:
            out.note(f"resuming from {resume.path}")
        args = build_train_args(cfg, dataset_sha, resume)
        final = run_lerobot_train(cfg, lerobot_train, args, env, resumed_from, out, signals)

    local_path = str(final.pretrained_dir)
    if not cfg.push:
        out.event("done", model="", revision="", local_path=local_path)
        return

    card = build_model_card(
        cfg,
        dataset_sha,
        dataset_info,
        final.step,
        env.get("LEROBOT_TRAIN_IMAGE", "").strip() or DEFAULT_IMAGE,
        env.get("LEROBOT_TRAIN_IMAGE_REVISION", "").strip() or "unknown",
        version,
    )
    message = f"Train {cfg.policy} on {cfg.dataset}@{dataset_sha[:12]} for {final.step} steps (wendy train)"
    out.event("pushing", model=cfg.model_repo)
    # The card quotes WENDY_TRAIN_EXTRA_ARGS, so it is scrubbed like the log.
    revision = push_with_retries(hub, cfg, final.pretrained_dir, out.redact(card), message, out)
    out.event("done", model=cfg.model_repo, revision=revision, local_path=local_path)


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    hub_factory: Callable[[str | None], Hub] = Hub,
) -> int:
    argv = sys.argv[1:] if argv is None else argv
    env = dict(os.environ) if env is None else dict(env)
    out = Output(Redactor(env), stdout or sys.stdout, stderr or sys.stderr)
    signals = Signals()
    try:
        signals.install()
        if argv:
            raise InputError("the entrypoint takes no arguments; configure it with WENDY_TRAIN_* environment variables")
        cfg = parse_config(env)
        train(cfg, env, out, signals, hub_factory)
        return 0
    except TrainError as exc:
        out.event("error", message=str(exc))
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - every failure must end in an error event
        for line in traceback.format_exc().splitlines():
            out.note(line)
        out.event("error", message=f"internal error: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
