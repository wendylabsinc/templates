#!/usr/bin/env python3
"""Small Wendy entrypoint for IsaacLab RSL-RL training."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PROFILE_DEFAULTS = {
    "unitree-go2-flat": {
        "task": "Isaac-Velocity-Flat-Unitree-Go2-v0",
        "experiment": "unitree_go2_velocity_flat",
    },
    "unitree-go2-rough": {
        "task": "Isaac-Velocity-Rough-Unitree-Go2-v0",
        "experiment": "unitree_go2_velocity_rough",
    },
    "cartpole-smoke": {
        "task": "Isaac-Cartpole-v0",
        "experiment": "cartpole_smoke",
    },
}


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def find_isaaclab_root() -> Path:
    for candidate in (os.environ.get("ISAACLAB_ROOT"), "/workspace/IsaacLab", "/IsaacLab"):
        if not candidate:
            continue
        root = Path(candidate)
        if (root / "isaaclab.sh").exists():
            return root
    raise FileNotFoundError("Set ISAACLAB_ROOT to the directory containing isaaclab.sh.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch IsaacLab RSL-RL training on Wendy.")
    parser.add_argument("--robot-profile", default=os.environ.get("WENDY_ROBOT_PROFILE", "unitree-go2-flat"))
    parser.add_argument("--task", default=os.environ.get("WENDY_TRAIN_TASK", "auto"))
    parser.add_argument("--num-envs", default=os.environ.get("WENDY_NUM_ENVS", "1024"))
    parser.add_argument("--max-iterations", default=os.environ.get("WENDY_MAX_ITERATIONS", "20000"))
    parser.add_argument("--experiment-name", default=os.environ.get("WENDY_EXPERIMENT_NAME", "auto"))
    parser.add_argument("--run-name", default=os.environ.get("WENDY_RUN_NAME"))
    parser.add_argument("--headless", action="store_true", default=env_bool("WENDY_HEADLESS", True))
    parser.add_argument("--no-headless", action="store_false", dest="headless")
    parser.add_argument("--record-video", action="store_true", default=env_bool("WENDY_RECORD_VIDEO", True))
    parser.add_argument("--no-record-video", action="store_false", dest="record_video")
    parser.add_argument("--video-length", default=os.environ.get("WENDY_VIDEO_LENGTH", "300"))
    parser.add_argument("--video-interval", default=os.environ.get("WENDY_VIDEO_INTERVAL"))
    parser.add_argument("--auto-resume", action="store_true", default=env_bool("WENDY_AUTO_RESUME", True))
    parser.add_argument("--no-auto-resume", action="store_false", dest="auto_resume")
    parser.add_argument("extra_args", nargs="*", help="Extra args passed to IsaacLab's RSL-RL trainer.")
    return parser.parse_args(argv)


def resolve_profile(args: argparse.Namespace) -> None:
    profile = str(args.robot_profile).strip().lower()
    defaults = PROFILE_DEFAULTS.get(profile)

    if args.task == "auto":
        if defaults is None:
            raise SystemExit(
                f"ROBOT_PROFILE={args.robot_profile!r} has no built-in task. "
                "Set WENDY_TRAIN_TASK to a registered IsaacLab task id."
            )
        args.task = defaults["task"]

    if args.experiment_name == "auto":
        if defaults is None:
            safe_profile = profile.replace("_", "-").replace(" ", "-") or "custom"
            args.experiment_name = f"{safe_profile}_rl"
        else:
            args.experiment_name = defaults["experiment"]


def checkpoint_iteration(path: Path) -> int:
    try:
        return int(path.stem.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return -1


def latest_resume_args(log_root: Path, experiment_name: str) -> list[str]:
    exp_root = log_root / "rsl_rl" / experiment_name
    if not exp_root.exists():
        return []
    candidates: list[tuple[int, float, Path, Path]] = []
    for run_dir in exp_root.iterdir():
        if not run_dir.is_dir():
            continue
        for checkpoint in run_dir.glob("model_*.pt"):
            iteration = checkpoint_iteration(checkpoint)
            if iteration > 0:
                candidates.append((iteration, checkpoint.stat().st_mtime, run_dir, checkpoint))
    if not candidates:
        return []
    _, _, run_dir, checkpoint = max(candidates, key=lambda item: (item[0], item[1]))
    return ["--resume", "--load_run", run_dir.name, "--checkpoint", checkpoint.name]


def prepare_logs(app_root: Path) -> Path:
    log_root = Path(os.environ.get("WENDY_LOG_ROOT", "/logs"))
    log_root.mkdir(parents=True, exist_ok=True)

    workspace_logs = app_root / "logs"
    if workspace_logs.is_symlink():
        if workspace_logs.resolve() != log_root:
            workspace_logs.unlink()
            workspace_logs.symlink_to(log_root, target_is_directory=True)
    elif workspace_logs.exists():
        if not workspace_logs.is_dir() or any(workspace_logs.iterdir()):
            print(f"[wendy-isaac] using persistent log root {log_root}; leaving {workspace_logs} unchanged", flush=True)
        else:
            workspace_logs.rmdir()
            workspace_logs.symlink_to(log_root, target_is_directory=True)
    else:
        workspace_logs.symlink_to(log_root, target_is_directory=True)

    marker = log_root / "wendy_isaac_training_marker.txt"
    marker.write_text(
        f"created_utc={datetime.now(timezone.utc).isoformat()}\n"
        f"app_root={app_root}\n"
        f"log_root={log_root}\n",
        encoding="utf-8",
    )
    return log_root


def build_command(args: argparse.Namespace, app_root: Path, log_root: Path) -> list[str]:
    root = find_isaaclab_root()
    wrapper = app_root / "scripts" / "run_isaaclab_train.py"
    experience = root / "apps" / ("isaaclab.python.headless.rendering.kit" if args.record_video else "isaaclab.python.headless.kit")

    cmd = [
        str(root / "isaaclab.sh"),
        "-p",
        str(wrapper),
        "--task",
        args.task,
        "--num_envs",
        str(args.num_envs),
        "--max_iterations",
        str(args.max_iterations),
        "--experiment_name",
        args.experiment_name,
        "--livestream",
        "0",
        "--experience",
        str(experience),
    ]
    if args.run_name:
        cmd += ["--run_name", args.run_name]
    if args.headless:
        cmd.append("--headless")
    if args.record_video:
        cmd += ["--video", "--video_length", str(args.video_length)]
        if args.video_interval:
            cmd += ["--video_interval", str(args.video_interval)]
    if args.auto_resume:
        resume = latest_resume_args(log_root, args.experiment_name)
        if resume:
            print("[wendy-isaac] resuming from latest checkpoint:", shlex.join(resume), flush=True)
            cmd.extend(resume)
    cmd.extend(args.extra_args)
    return cmd


def main() -> int:
    app_root = Path(__file__).resolve().parent
    args = parse_args(sys.argv[1:])
    resolve_profile(args)
    log_root = prepare_logs(app_root)

    env = os.environ.copy()
    source_root = app_root / "source"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(source_root / "local_isaac_tasks"), str(source_root), env.get("PYTHONPATH", "")]
    )

    cmd = build_command(args, app_root, log_root)
    print("[wendy-isaac] robot profile:", args.robot_profile, flush=True)
    print("[wendy-isaac] task:", args.task, flush=True)
    print("[wendy-isaac] experiment:", args.experiment_name, flush=True)
    print("[wendy-isaac] log root:", log_root, flush=True)
    print("[wendy-isaac] starting:", shlex.join(cmd), flush=True)
    return subprocess.call(cmd, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
