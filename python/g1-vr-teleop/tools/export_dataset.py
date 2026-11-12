#!/usr/bin/env python3
"""Export recorded teleop sessions as a GR00T and/or LeRobot training dataset.

    python3 tools/pull_sessions.py --robot <robot>:8443 --out sessions/
    python3 tools/export_dataset.py sessions/*.manifest.json --output datasets/pick-cup \\
        --format both --task "Pick up the cup and place it on the plate"

Formats:
  groot       GR00T LeRobot: LeRobot v2.1 + meta/modality.json + new_embodiment_config.py.
              What Isaac-GR00T fine-tunes on. Needs numpy, pyarrow, Pillow, ffmpeg.
  lerobot-v3  LeRobot's current format, made by LeRobot's own converter
              (lerobot.scripts.convert_dataset_v21_to_v30) from the v2.1 export.
              Also needs: pip install 'lerobot[dataset]'.
  both        <output>/groot and <output>/lerobot-v3 from the same episodes.

One dataset, one task string, episode indices continuing across sessions.
--task may be omitted when every session's header carries the same task.
The logic is teleop/bridge/groot_export.py.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "teleop", "bridge"))
import groot_export as ge  # noqa: E402

CONVERTER = "lerobot.scripts.convert_dataset_v21_to_v30"


def lerobot_available(python):
    try:
        probe = subprocess.run([python, "-c", f"import importlib; importlib.import_module('{CONVERTER}')"],
                               capture_output=True, text=True)
    except OSError as e:
        return False, f"cannot run {python}: {e.strerror}"
    return probe.returncode == 0, (probe.stderr.strip().splitlines() or [""])[-1]


def to_lerobot_v3(v21_dir, out_dir, python, repo_id):
    """LeRobot's converter works in place and leaves <dir>_old beside it; run it
    on a copy so the v2.1 export (the GR00T one) is never touched."""
    out_dir = Path(out_dir)
    shutil.copytree(v21_dir, out_dir)
    cmd = [python, "-m", CONVERTER, f"--repo-id={repo_id}", f"--root={out_dir}", "--push-to-hub=false"]
    run = subprocess.run(cmd, capture_output=True, text=True)
    old = out_dir.parent / f"{out_dir.name}_old"
    if run.returncode != 0:
        shutil.rmtree(out_dir, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
        raise ge.ExportRefused(["LeRobot v3 conversion failed: " + (run.stderr.strip()[-800:] or run.stdout[-800:])])
    shutil.rmtree(old, ignore_errors=True)
    # The provenance record is ours, not LeRobot's: carry it across.
    shutil.copy2(Path(v21_dir) / "wendy-export.json", out_dir / "wendy-export.json")
    info = json.loads((out_dir / "meta" / "info.json").read_text())
    return info.get("codebase_version")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("manifest", nargs="+", help="<sid>.manifest.json sealed by the bridge")
    ap.add_argument("--output", required=True, help="dataset directory; must not exist")
    ap.add_argument("--format", choices=("groot", "lerobot-v3", "both"), default="groot")
    ap.add_argument("--task", default=None,
                    help="the language instruction for every episode (default: the sessions' header task)")
    ap.add_argument("--fps", type=int, default=ge.DEFAULT_FPS)
    ap.add_argument("--outcomes", default="all",
                    help="'all' (default), or comma-separated offline-label results to keep, e.g. success")
    ap.add_argument("--robot-type", default=ge.DEFAULT_ROBOT_TYPE)
    ap.add_argument("--repo-id", default=None,
                    help="LeRobot repo id recorded in the v3 dataset (default: local/<output name>)")
    ap.add_argument("--lerobot-python", default=sys.executable,
                    help="python that has lerobot[dataset] installed (default: this one)")
    ap.add_argument("--action-from-targets", action="store_true",
                    help="use pre-offset `targets` for the arm action when `sent` is absent (recorded as a caveat)")
    ap.add_argument("--allow-missing-hands", action="store_true",
                    help="zero-fill a hand with no measured joints (recorded as a caveat)")
    ap.add_argument("--allow-missing-frames", action="store_true",
                    help="export state/action only when no camera frames were recorded (debugging only)")
    args = ap.parse_args(argv)
    outcomes = None if args.outcomes.strip() == "all" else frozenset(
        s.strip() for s in args.outcomes.split(",") if s.strip())
    output = Path(args.output)
    if output.exists():
        print(f"REFUSED: nothing written\n  output already exists: {output}")
        return 1
    if args.format != "groot":
        ok, why = lerobot_available(args.lerobot_python)
        if not ok:
            print("REFUSED: nothing written\n  lerobot-v3 needs LeRobot: pip install 'lerobot[dataset]' "
                  f"(or pass --lerobot-python); {why}")
            return 1
    groot_dir = output / "groot" if args.format == "both" else output
    if args.format == "lerobot-v3":
        groot_dir = output.parent / f".{output.name}.v21"
        shutil.rmtree(groot_dir, ignore_errors=True)
    try:
        record = ge.export(args.manifest, groot_dir, args.task, fps=args.fps, outcomes=outcomes,
                           robot_type=args.robot_type, action_from_targets=args.action_from_targets,
                           allow_missing_hands=args.allow_missing_hands,
                           allow_missing_frames=args.allow_missing_frames)
        if args.format != "groot":
            v3 = output / "lerobot-v3" if args.format == "both" else output
            repo_id = args.repo_id or f"local/{output.name}"
            version = to_lerobot_v3(groot_dir, v3, args.lerobot_python, repo_id)
    except ge.ExportRefused as e:
        if args.format == "both":
            shutil.rmtree(output, ignore_errors=True)
        print("REFUSED: nothing written")
        for r in e.reasons:
            print(f"  {r}")
        return 1
    finally:
        if args.format == "lerobot-v3":
            shutil.rmtree(groot_dir, ignore_errors=True)
    if not record["groot_format"]["video_key"]:
        print("WARNING: exported without video; GR00T and LeRobot policies need the camera. Debugging only.")
    print(ge.summary_line(record))
    if args.format in ("groot", "both"):
        print(f"  GR00T LeRobot v2.1: {groot_dir}")
    if args.format in ("lerobot-v3", "both"):
        print(f"  LeRobot {version}: {output / 'lerobot-v3' if args.format == 'both' else output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
