#!/usr/bin/env python3
"""SO-101 demonstration recorder: a headless lerobot-record for Wendy.

You move the leader arm by hand, the follower arm copies it, and every frame
(follower joint positions, the leader's command and the camera image) goes into
a LeRobotDataset in LeRobot 0.6.1's format (codebase v3.0). Recording uses
LeRobot's own code: LeRobotDataset.create/resume, record_loop from
lerobot-record, and its video encoder. Two things differ from lerobot-record:

- lerobot-record reads arrow keys from a terminal, and a Wendy container has
  none, so the same controls (next, re-record, stop) are buttons on a web page.
- Each episode is saved and the dataset closed (finalized) before the next one
  starts, so stopping the app, or losing power, costs at most the episode in
  progress. The next start resumes the same dataset.

The dataset lives in the app's persistent volume at $HF_LEROBOT_HOME/<repo_id>
(/data/lerobot/<repo_id>). When HF_TOKEN is set and the repo id is not
local/..., the finished dataset is uploaded with LeRobot's push_to_hub.
Settings come from the environment: see record_settings.py and the README.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import record_settings
import so101_common as common

log = logging.getLogger("so101-record")

# Decode with PyAV, which bundles its own FFmpeg. torchcodec (LeRobot's other
# decoder) needs a system FFmpeg that the image does not install.
VIDEO_BACKEND = "pyav"
# How long the follower takes to reach the leader's pose after connecting.
HANDOVER_SECONDS = 2.0


class Recorder:
    def __init__(self, settings, settings_error=None):
        self.s = settings
        self.lock = threading.Lock()
        # The dict lerobot-record's keyboard listener fills in; the web page sets it here.
        self.events = {"exit_early": False, "rerecord_episode": False, "stop_recording": False}
        self.terminating = False
        self.state = "starting"
        self.detail = ""
        self.error = str(settings_error) if settings_error else None
        self.report = []
        self.saved_episodes = None
        self.session_episodes = 0
        self.phase_ends = None
        self.push_state = None
        self.calibration = {}

    # ── status and controls (called from the HTTP threads) ─────────────────

    def set_state(self, state, detail="", seconds=None):
        with self.lock:
            self.state = state
            self.detail = detail
            self.phase_ends = time.monotonic() + seconds if seconds is not None else None
        log.info("%s%s", state, f": {detail}" if detail else "")

    def status(self):
        s = self.s
        with self.lock:
            remaining = None
            if self.phase_ends is not None:
                remaining = max(0.0, round(self.phase_ends - time.monotonic(), 1))
            return {
                "state": self.state,
                "detail": self.detail,
                "error": self.error,
                "report": list(self.report),
                "seconds_left": remaining,
                "saved_episodes": self.saved_episodes,
                "session_episodes": self.session_episodes,
                "target_episodes": s.num_episodes if s else None,
                "task": s.task if s else None,
                "repo_id": s.repo_id if s else None,
                "dataset_path": str(s.dataset_root) if s else None,
                "upload": s.push_reason if s else None,
                "push_state": self.push_state,
                "can_push": bool(s and s.push and self.state == "done" and self.push_state != "uploading"),
                "dry_run": bool(s and s.dry_run),
            }

    def _require_phase(self):
        if self.state not in ("recording", "resetting"):
            raise common.SettingsError(f"nothing to control while {self.state}")

    def next(self):
        self._require_phase()
        self.events["exit_early"] = True
        return "ending this phase"

    def rerecord(self):
        self._require_phase()
        self.events["rerecord_episode"] = True
        self.events["exit_early"] = True
        return "discarding this episode; it will be recorded again"

    def stop(self):
        self.events["stop_recording"] = True
        self.events["exit_early"] = True
        return "stopping after this phase"

    def terminate(self):
        self.terminating = True
        self.stop()

    def push(self):
        if not (self.s and self.s.push):
            raise common.SettingsError(self.s.push_reason if self.s else "settings are invalid")
        with self.lock:
            if self.state != "done" or self.push_state == "uploading":
                raise common.SettingsError("upload is possible once the session has finished")
            self.push_state = "uploading"
        threading.Thread(target=self._push, name="push", daemon=True).start()
        return "uploading"

    # ── the recording session (main thread) ────────────────────────────────

    def run(self):
        if self.s is None:
            self.set_state("error", "fix the settings and run the app again")
            return
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001 - keep serving the page with the error
            log.error("%s", traceback.format_exc())
            with self.lock:
                self.error = f"{type(exc).__name__}: {exc}"
            self.set_state("error", "see the log: wendy device logs")

    def _run(self):
        s = self.s
        self.set_state("loading", "importing LeRobot")
        lerobot = load_lerobot()
        calibration_errors = self._load_calibration()
        if s.dry_run:
            self._dry_run(lerobot, calibration_errors)
            return
        if calibration_errors:
            raise common.SettingsError(" ".join(calibration_errors))
        robot, teleop = make_devices(lerobot, s)
        features = dataset_features(lerobot, robot)

        state = record_settings.dataset_state(s.dataset_root)
        if state == "foreign":
            raise common.SettingsError(
                f"{s.dataset_root} exists but is not a LeRobot dataset (no readable meta/info.json). "
                "Move it away, or use another SO101_DATASET_REPO_ID."
            )
        saved = count_episodes(s.dataset_root) if state == "resume" else 0
        with self.lock:
            self.saved_episodes = saved
        if saved >= s.num_episodes:
            self.set_state("done", f"{saved} episodes already recorded (SO101_NUM_EPISODES={s.num_episodes})")
            return
        if self.terminating:
            return

        self.set_state("connecting", f"leader {s.leader_port}, follower {s.follower_port}")
        try:
            # Same order as lerobot-record: the leader first, so the follower is
            # not left idle (and possibly tripping a watchdog) while it initializes.
            teleop.connect()
            robot.connect()
            # Bring the follower to the leader's pose slowly, instead of letting
            # the first teleoperated frame jump it there at full speed.
            self.set_state("connecting", "moving the follower to the leader's pose")
            common.move_smoothly(robot, teleop.get_action(), HANDOVER_SECONDS, fps=s.fps,
                                 should_stop=lambda: self.terminating)
            self._session(lerobot, robot, teleop, features)
        finally:
            self.set_state("finishing", "releasing the arms")
            common.release(robot)  # torque off: the follower goes limp, as in lerobot-record
            common.release(teleop)
        saved = count_episodes(s.dataset_root)
        with self.lock:
            self.saved_episodes = saved
        self.set_state("done", f"{saved} episodes in {s.dataset_root}")
        if s.push and self.session_episodes > 0 and not self.terminating:
            with self.lock:
                self.push_state = "uploading"
            self._push()

    def _load_calibration(self):
        s = self.s
        errors = []
        for role, path, variable in (
            ("follower", s.follower_calibration, "SO101_FOLLOWER_CALIBRATION"),
            ("leader", s.leader_calibration, "SO101_LEADER_CALIBRATION"),
        ):
            try:
                self.calibration[role] = common.load_calibration(path, role, variable)
                for warning in self.calibration[role].warnings:
                    log.warning("%s calibration: %s", role, warning)
            except common.SettingsError as exc:
                errors.append(str(exc))
        return errors

    def _session(self, lerobot, robot, teleop, features):
        """lerobot-record's episode loop, one dataset checkpoint per episode."""
        s = self.s
        teleop_proc, robot_proc, obs_proc = lerobot.make_default_processors()

        def loop(seconds, dataset=None):
            lerobot.record_loop(
                robot=robot,
                events=self.events,
                fps=s.fps,
                teleop_action_processor=teleop_proc,
                robot_action_processor=robot_proc,
                robot_observation_processor=obs_proc,
                dataset=dataset,
                teleop=teleop,
                control_time_s=seconds,
                single_task=s.task,
            )

        first = True
        while not self.events["stop_recording"]:
            dataset = open_dataset(lerobot, s, robot, features, check=first)
            if dataset.num_episodes >= s.num_episodes:
                dataset.finalize()
                break
            # VideoEncodingManager finalizes the dataset on exit (also on an
            # exception), which writes the parquet footers and closes the videos.
            with lerobot.VideoEncodingManager(dataset):
                number = dataset.num_episodes + 1
                if first:
                    # The follower now copies the leader. Give the operator one
                    # reset period to get the scene ready before episode 1.
                    first = False
                    self.set_state("resetting", f"get ready: episode {number} starts next", s.reset_seconds)
                    loop(s.reset_seconds)
                    if self.events["stop_recording"]:
                        break
                    self.events["rerecord_episode"] = False

                self.set_state("recording", f"episode {number} of {s.num_episodes}", s.episode_seconds)
                loop(s.episode_seconds, dataset)
                if self.terminating:
                    # The app is being stopped mid-episode: drop the partial episode.
                    dataset.clear_episode_buffer()
                    break
                last = number >= s.num_episodes
                if not self.events["stop_recording"] and (not last or self.events["rerecord_episode"]):
                    following = number if self.events["rerecord_episode"] else number + 1
                    self.set_state("resetting", f"reset the scene for episode {following}", s.reset_seconds)
                    loop(s.reset_seconds)
                if self.events["rerecord_episode"]:
                    self.events["rerecord_episode"] = False
                    self.events["exit_early"] = False
                    dataset.clear_episode_buffer()
                    log.info("episode %d discarded; recording it again", number)
                    continue
                if not dataset.has_pending_frames():
                    continue
                # Like lerobot-record, Stop keeps the episode in progress.
                self.set_state("saving", f"episode {number}")
                dataset.save_episode()
                with self.lock:
                    self.saved_episodes = dataset.num_episodes
                    self.session_episodes += 1
                log.info("saved episode %d (%d in the dataset)", number, dataset.num_episodes)

    def _push(self):
        s = self.s
        log.info("uploading %s to the Hugging Face Hub", s.repo_id)
        try:
            lerobot = load_lerobot()
            dataset = lerobot.LeRobotDataset(s.repo_id, root=s.dataset_root, video_backend=VIDEO_BACKEND)
            # push_to_hub reads HF_TOKEN from the environment (huggingface_hub's default).
            dataset.push_to_hub(private=s.private, tags=["so101", "wendy"])
            result = f"uploaded {dataset.num_episodes} episodes to https://huggingface.co/datasets/{s.repo_id}"
        except Exception as exc:  # noqa: BLE001
            log.error("%s", traceback.format_exc())
            result = f"upload failed: {type(exc).__name__}: {exc}. The dataset is still on the device."
        with self.lock:
            self.push_state = result
        log.info("%s", result)

    # ── dry run: check everything without moving anything ──────────────────

    def _dry_run(self, lerobot, calibration_errors):
        s = self.s
        report = []

        def add(line):
            report.append(line)
            log.info("dry run: %s", line)
            with self.lock:
                self.report = list(report)

        self.set_state("checking", "dry run: no dataset is written and nothing is written to the servos")
        add(f"LeRobot {lerobot.version}, dataset format {lerobot.CODEBASE_VERSION}")
        for message in calibration_errors:
            add(f"calibration: {message}")
        for role, cal in self.calibration.items():
            add(f"{role} calibration {cal.path}: ok" + (f" ({'; '.join(cal.warnings)})" if cal.warnings else ""))
        state = record_settings.dataset_state(s.dataset_root)
        add(f"dataset {s.dataset_root}: {state}" + (
            f", {count_episodes(s.dataset_root)} episodes" if state == "resume" else ""))
        try:
            # Building the devices opens nothing: no port, no camera.
            robot, _teleop = make_devices(lerobot, s)
            features = dataset_features(lerobot, robot)
        except Exception as exc:  # noqa: BLE001 - for example an unreadable calibration file
            add(f"could not build the LeRobot devices: {type(exc).__name__}: {common.first_line(exc)}")
        else:
            add(f"robot type {robot.name}")
            for name, feature in features.items():
                add(f"feature {name}: {feature['dtype']} {tuple(feature['shape'])}")
            add(synthetic_episode(lerobot, s, robot, features))
        add(common.probe_arm(s.follower_port, self.calibration.get("follower")))
        add(common.probe_arm(s.leader_port, self.calibration.get("leader")))
        for name, device in s.cameras.items():
            add(common.probe_camera(name, device, s.camera_width, s.camera_height, s.fps, s.camera_fourcc))
        add(s.push_reason)
        self.set_state("dry-run done", "set SO101_DRY_RUN=false to record")


class LeRobot:
    """The LeRobot 0.6.1 names this app uses, imported once (the torch import is slow)."""

    def __init__(self):
        import lerobot
        from lerobot.cameras.opencv import OpenCVCameraConfig
        from lerobot.common.control_utils import sanity_check_dataset_robot_compatibility
        from lerobot.configs.video import RGBEncoderConfig
        from lerobot.datasets import (
            CODEBASE_VERSION,
            LeRobotDataset,
            VideoEncodingManager,
            aggregate_pipeline_dataset_features,
            create_initial_features,
        )
        from lerobot.processor import make_default_processors
        from lerobot.robots.so_follower import SO101FollowerConfig, SOFollower
        from lerobot.scripts.lerobot_record import record_loop
        from lerobot.teleoperators.so_leader import SO101LeaderConfig, SOLeader
        from lerobot.utils.constants import ACTION, OBS_STR
        from lerobot.utils.feature_utils import build_dataset_frame, combine_feature_dicts

        self.version = lerobot.__version__
        self.CODEBASE_VERSION = CODEBASE_VERSION
        self.OpenCVCameraConfig = OpenCVCameraConfig
        self.sanity_check_dataset_robot_compatibility = sanity_check_dataset_robot_compatibility
        self.RGBEncoderConfig = RGBEncoderConfig
        self.LeRobotDataset = LeRobotDataset
        self.VideoEncodingManager = VideoEncodingManager
        self.aggregate_pipeline_dataset_features = aggregate_pipeline_dataset_features
        self.create_initial_features = create_initial_features
        self.make_default_processors = make_default_processors
        self.SO101FollowerConfig = SO101FollowerConfig
        self.SO101LeaderConfig = SO101LeaderConfig
        self.Follower = common.without_prompts(SOFollower, hold_on_connect=True)
        self.Leader = common.without_prompts(SOLeader, hold_on_connect=False)
        self.record_loop = record_loop
        self.ACTION = ACTION
        self.OBS_STR = OBS_STR
        self.build_dataset_frame = build_dataset_frame
        self.combine_feature_dicts = combine_feature_dicts


_LEROBOT = None


def load_lerobot():
    global _LEROBOT
    if _LEROBOT is None:
        _LEROBOT = LeRobot()
    return _LEROBOT


def make_devices(lerobot, s):
    """The follower (with its cameras) and the leader, built as lerobot-record builds them."""
    cameras = {
        name: lerobot.OpenCVCameraConfig(
            index_or_path=common.camera_index_or_path(device),
            fps=s.fps,
            width=s.camera_width,
            height=s.camera_height,
            fourcc=s.camera_fourcc,
        )
        for name, device in s.cameras.items()
    }
    # LeRobot looks for <calibration_dir>/<id>.json, so the file name is the arm's id.
    robot = lerobot.Follower(
        lerobot.SO101FollowerConfig(
            port=s.follower_port,
            id=s.follower_calibration.stem,
            calibration_dir=s.follower_calibration.parent,
            cameras=cameras,
            use_degrees=s.use_degrees,
        )
    )
    teleop = lerobot.Leader(
        lerobot.SO101LeaderConfig(
            port=s.leader_port,
            id=s.leader_calibration.stem,
            calibration_dir=s.leader_calibration.parent,
            use_degrees=s.use_degrees,
        )
    )
    return robot, teleop


def dataset_features(lerobot, robot):
    """The dataset schema, built exactly as lerobot-record builds it."""
    teleop_proc, _robot_proc, obs_proc = lerobot.make_default_processors()
    return lerobot.combine_feature_dicts(
        lerobot.aggregate_pipeline_dataset_features(
            pipeline=teleop_proc,
            initial_features=lerobot.create_initial_features(action=robot.action_features),
            use_videos=True,
        ),
        lerobot.aggregate_pipeline_dataset_features(
            pipeline=obs_proc,
            initial_features=lerobot.create_initial_features(observation=robot.observation_features),
            use_videos=True,
        ),
    )


def writer_options(lerobot, s, num_cameras):
    return {
        "rgb_encoder": lerobot.RGBEncoderConfig(vcodec=s.video_codec),
        "streaming_encoding": s.streaming_encoding,
        "encoder_threads": s.encoder_threads,
        # Without streaming encoding, frames are first written as PNGs;
        # lerobot-record uses 4 writer threads per camera for that.
        "image_writer_threads": 0 if s.streaming_encoding else 4 * num_cameras,
        "video_backend": VIDEO_BACKEND,
    }


def count_episodes(root):
    """Episodes saved in the dataset at `root`; 0 when there is no dataset yet."""
    try:
        info = json.loads((Path(root) / "meta" / "info.json").read_text())
        return int(info.get("total_episodes") or 0)
    except (OSError, ValueError, AttributeError):
        return 0


def open_dataset(lerobot, s, robot, features, check):
    """Resume the dataset in the volume, or create it. Never renames the repo id.

    lerobot-record adds a date-time stamp to the repo id of every new dataset;
    this app keeps SO101_DATASET_REPO_ID as given, so a restart resumes the
    same dataset and the name you train on is the name you set.
    """
    root = s.dataset_root
    options = writer_options(lerobot, s, len(robot.cameras))
    state = record_settings.dataset_state(root)
    if state == "resume":
        dataset = lerobot.LeRobotDataset.resume(s.repo_id, root=root, **options)
        if check:
            try:
                lerobot.sanity_check_dataset_robot_compatibility(dataset, robot, s.fps, features)
            except ValueError as exc:
                dataset.finalize()
                raise common.SettingsError(
                    f"{root} was recorded with different settings, so new episodes cannot be added to it "
                    f"({exc}). Use a new SO101_DATASET_REPO_ID, or restore the old settings."
                ) from None
            log.info("resuming %s: %d episodes recorded so far", root, dataset.num_episodes)
        return dataset
    if state == "foreign":
        raise common.SettingsError(f"{root} is not a LeRobot dataset; use another SO101_DATASET_REPO_ID")
    if state == "empty":
        log.info("%s has no saved episode; starting it again", root)
        shutil.rmtree(root)
    elif root.exists():
        root.rmdir()  # "new": an empty directory; LeRobot creates the root itself
    log.info("creating %s", root)
    return lerobot.LeRobotDataset.create(
        s.repo_id,
        s.fps,
        root=root,
        robot_type=robot.name,
        features=features,
        use_videos=True,
        **options,
    )


def synthetic_episode(lerobot, s, robot, features):
    """Write a 2-second episode of generated frames to a scratch dataset and read it back."""
    import numpy as np

    scratch = Path(tempfile.mkdtemp(prefix="so101-dry-run-"))
    try:
        dataset = lerobot.LeRobotDataset.create(
            s.repo_id,
            s.fps,
            root=scratch / "dataset",
            robot_type=robot.name,
            features=features,
            use_videos=True,
            **writer_options(lerobot, s, len(s.cameras)),
        )
        with lerobot.VideoEncodingManager(dataset):
            for index in range(2 * s.fps):
                values = {f"{joint}.pos": float(index % 20) for joint in common.JOINTS}
                observation = dict(values)
                for name in s.cameras:
                    row = (np.arange(s.camera_width, dtype=np.uint16) + 4 * index) % 256
                    observation[name] = np.broadcast_to(
                        row.astype(np.uint8)[None, :, None], (s.camera_height, s.camera_width, 3)
                    ).copy()
                frame = {
                    **lerobot.build_dataset_frame(features, observation, prefix=lerobot.OBS_STR),
                    **lerobot.build_dataset_frame(features, values, prefix=lerobot.ACTION),
                    "task": s.task,
                }
                dataset.add_frame(frame)
            dataset.save_episode()
        readback = lerobot.LeRobotDataset(s.repo_id, root=scratch / "dataset", video_backend=VIDEO_BACKEND)
        item = readback[len(readback) - 1]
        images = {key: tuple(item[key].shape) for key in item if key.startswith("observation.images.")}
        return (
            f"scratch dataset: wrote and read back {len(readback)} frames in {readback.num_episodes} episode "
            f"({s.video_codec} video, images {images})"
        )
    except Exception as exc:  # noqa: BLE001 - report it; the rest of the dry run still runs
        log.error("%s", traceback.format_exc())
        return f"scratch dataset FAILED: {type(exc).__name__}: {common.first_line(exc)}"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


PAGE = """<!doctype html><html><head><meta charset=utf-8><title>SO-101 recorder</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{font-family:ui-monospace,Menlo,monospace;background:#0c0c0c;color:#e8e8e8;margin:20px;max-width:900px}
button{font-size:16px;padding:10px 16px;margin:6px 6px 6px 0}.muted{color:#888}.err{color:#f66;white-space:pre-wrap}
.state{font-size:22px;margin:10px 0}.recording{color:#f55}.resetting{color:#fc5}.done{color:#7f7}
pre{background:#000;border:1px solid #333;padding:10px;white-space:pre-wrap}</style></head><body>
<h2>SO-101 recorder</h2>
<div class=muted>Move the leader arm; the follower copies it. Recording follows LeRobot's lerobot-record loop.</div>
<div id=state class=state>...</div>
<div id=episodes></div><div id=task class=muted></div>
<div>
<button onclick="send('next')" title="lerobot-record: right arrow">Next: end this phase now</button>
<button onclick="send('rerecord')" title="lerobot-record: left arrow">Re-record this episode</button>
<button onclick="send('stop')" title="lerobot-record: Esc">Stop recording</button>
<button id=push onclick="send('push')">Upload to the Hub</button>
</div>
<div id=err class=err></div>
<pre id=info></pre>
<script>
async function send(action){
  const r = await fetch('/api/' + action, {method: 'POST'});
  const body = await r.json();
  document.getElementById('err').textContent = r.ok ? '' : body.error;
  poll();
}
async function poll(){
  let d; try { d = await (await fetch('/api/status')).json(); } catch (e) { return; }
  const st = document.getElementById('state');
  const left = d.seconds_left === null ? '' : ' (' + Math.ceil(d.seconds_left) + ' s left)';
  st.className = 'state ' + d.state;
  st.textContent = d.state + (d.detail ? ': ' + d.detail : '') + left;
  document.getElementById('episodes').textContent = d.saved_episodes === null ? '' :
    'episodes saved: ' + d.saved_episodes + ' of ' + d.target_episodes + ' (' + d.session_episodes + ' this session)';
  document.getElementById('task').textContent = d.task ? 'task: ' + d.task : '';
  document.getElementById('push').disabled = !d.can_push;
  if (d.error) document.getElementById('err').textContent = d.error;
  const lines = ['dataset: ' + d.repo_id, 'on the device: ' + d.dataset_path, 'upload: ' + d.upload];
  if (d.push_state) lines.push('upload status: ' + d.push_state);
  document.getElementById('info').textContent = lines.concat(d.report).join('\\n');
}
setInterval(poll, 500); poll();
</script></body></html>"""


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings, error = None, None
    try:
        settings = record_settings.load(os.environ)
    except common.SettingsError as exc:
        error = exc
        log.error("settings: %s", exc)
    recorder = Recorder(settings, error)
    port = settings.http_port if settings else int(os.environ.get("SO101_HTTP_PORT") or 8092)
    actions = {
        "next": recorder.next,
        "rerecord": recorder.rerecord,
        "stop": recorder.stop,
        "push": recorder.push,
    }
    common.ControlServer(port, PAGE, recorder.status, actions).start()
    log.info("SO-101 recorder page on http://0.0.0.0:%d", port)
    if settings:
        log.info(
            "dataset %s at %s; %s; follower %s, leader %s, cameras %s",
            settings.repo_id,
            settings.dataset_root,
            settings.push_reason,
            settings.follower_port,
            settings.leader_port,
            settings.cameras,
        )

    stopping = threading.Event()

    def on_signal(signum, _frame):
        if stopping.is_set():
            log.warning("second signal: exiting without cleanup")
            os._exit(1)
        log.info("signal %d: stopping", signum)
        stopping.set()
        recorder.terminate()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    recorder.run()
    # Stay up after the session so the page keeps showing the result; Wendy
    # restarts apps that exit, which would start another session.
    while not stopping.wait(1.0):
        pass
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
