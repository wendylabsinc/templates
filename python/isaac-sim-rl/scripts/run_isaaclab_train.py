#!/usr/bin/env python3
"""Register optional local IsaacLab tasks, then run IsaacLab's RSL-RL trainer."""

from __future__ import annotations

import faulthandler
import os
import runpy
import signal
import sys
from pathlib import Path


def find_isaaclab_root() -> Path:
    for candidate in (os.environ.get("ISAACLAB_ROOT"), "/workspace/IsaacLab", "/IsaacLab"):
        if not candidate:
            continue
        root = Path(candidate)
        if (root / "isaaclab.sh").exists():
            return root
    raise FileNotFoundError("Set ISAACLAB_ROOT to the directory containing isaaclab.sh.")


def main() -> None:
    try:
        faulthandler.enable(all_threads=True)
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
    except (AttributeError, RuntimeError, ValueError):
        pass

    # Import side effect: users can register custom Gymnasium task ids here.
    try:
        import local_isaac_tasks  # noqa: F401
    except Exception as exc:
        print(f"[wendy-isaac] local task import skipped: {exc}", flush=True)

    root = find_isaaclab_root()
    train_script = root / "scripts" / "reinforcement_learning" / "rsl_rl" / "train.py"
    if not train_script.exists():
        raise FileNotFoundError(f"Missing IsaacLab RSL-RL train script: {train_script}")

    sys.path.insert(0, str(train_script.parent))
    sys.argv = [str(train_script), *sys.argv[1:]]
    runpy.run_path(str(train_script), run_name="__main__")


if __name__ == "__main__":
    main()
