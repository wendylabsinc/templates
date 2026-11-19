#!/usr/bin/env python3
"""SO-101 policy runner: a trained LeRobot policy drives the follower arm.

Loads SO101_MODEL_PATH (a Hugging Face model id or a directory) at exactly
SO101_MODEL_REVISION, reads the policy type from the model's config (ACT or
SmolVLA), and runs it with LeRobot 0.6.1's own pieces: the policy class, the
pre- and post-processors saved with the model, predict_action, and the SO
follower robot. That is the loop lerobot-rollout's base strategy runs; this app
adds Start and Stop buttons on a web page (a container has no terminal) and a
safe stop: the arm returns to the pose it started from, then torque goes off.

The arm stays limp until you press Start (or set SO101_AUTOSTART=true).
Settings come from the environment: see policy_settings.py and the README.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass

import policy_settings
import so101_common as common

log = logging.getLogger("so101-policy")

# Dry run: inference passes timed on synthetic input, after one warm-up pass.
DRY_RUN_PASSES = 3


class LeRobot:
    """The LeRobot 0.6.1 names this app uses, imported once (the torch import is slow)."""

    def __init__(self):
        import lerobot
        import torch
        from lerobot.cameras.opencv import OpenCVCameraConfig
        from lerobot.common.control_utils import predict_action
        from lerobot.configs import PreTrainedConfig
        from lerobot.policies import get_policy_class, make_pre_post_processors
        from lerobot.policies.utils import make_robot_action
        from lerobot.processor import make_default_processors
        from lerobot.robots.so_follower import SO101FollowerConfig, SOFollower
        from lerobot.utils.constants import ACTION, OBS_STR
        from lerobot.utils.feature_utils import build_dataset_frame, combine_feature_dicts, hw_to_dataset_features
        from lerobot.utils.robot_utils import precise_sleep

        self.version = lerobot.__version__
        self.torch = torch
        self.OpenCVCameraConfig = OpenCVCameraConfig
        self.predict_action = predict_action
        self.PreTrainedConfig = PreTrainedConfig
        self.get_policy_class = get_policy_class
        self.make_pre_post_processors = make_pre_post_processors
        self.make_robot_action = make_robot_action
        self.make_default_processors = make_default_processors
        self.SO101FollowerConfig = SO101FollowerConfig
        self.Follower = common.without_prompts(SOFollower, hold_on_connect=True)
        self.ACTION = ACTION
        self.OBS_STR = OBS_STR
        self.build_dataset_frame = build_dataset_frame
        self.combine_feature_dicts = combine_feature_dicts
        self.hw_to_dataset_features = hw_to_dataset_features
        self.precise_sleep = precise_sleep


@dataclass
class LoadedPolicy:
    policy: object
    preprocessor: object
    postprocessor: object
    policy_type: str
    device: str
    cameras: dict  # camera name -> device path
    camera_sizes: dict  # camera name -> (width, height)


def hub_commit(repo_id, revision):
    """The commit a Hub revision points at now (HF_TOKEN from the environment is used if set)."""
    from huggingface_hub import HfApi

    return HfApi().model_info(repo_id, revision=revision).sha


def cached_commit(repo_id, revision):
    """The commit of a copy of the model already in the local cache, or None."""
    from huggingface_hub import try_to_load_from_cache

    path = try_to_load_from_cache(repo_id, "config.json", revision=revision)
    return policy_settings.commit_from_cache_path(path) if isinstance(path, str) else None


def feature_table(features):
    """{name: (type, shape)} from a LeRobot config's PolicyFeature dict."""
    return {name: (getattr(ft.type, "value", str(ft.type)), tuple(ft.shape)) for name, ft in (features or {}).items()}


class Runner:
    def __init__(self, settings, settings_error=None):
        self.s = settings
        self.lock = threading.Lock()
        self.state = "starting"
        self.detail = ""
        self.error = str(settings_error) if settings_error else None
        self.model = {}
        self.report = []
        self.stats = {"steps": 0, "hz": None, "inference_ms": None}
        self.start_requested = threading.Event()
        self.stop_requested = threading.Event()
        self.terminating = threading.Event()
        self.lr = None
        self.loaded = None

    # ── status and controls (called from the HTTP threads) ─────────────────

    def set_state(self, state, detail=""):
        with self.lock:
            self.state = state
            self.detail = detail
        log.info("%s%s", state, f": {detail}" if detail else "")

    def status(self):
        s = self.s
        with self.lock:
            return {
                "state": self.state,
                "detail": self.detail,
                "error": self.error,
                "model": dict(self.model),
                "report": list(self.report),
                "stats": dict(self.stats),
                "follower_port": s.follower_port if s else None,
                "fps": s.fps if s else None,
                "task": s.task if s else None,
                "dry_run": bool(s and s.dry_run),
                "can_start": self.state == "ready",
                "can_stop": self.state in ("starting", "running"),
            }

    def start(self):
        with self.lock:
            if self.state != "ready":
                raise common.SettingsError(f"cannot start while {self.state}")
            self.error = None
        self.stop_requested.clear()
        self.start_requested.set()
        return "starting"

    def stop(self):
        self.start_requested.clear()
        self.stop_requested.set()
        return "stopping"

    def terminate(self):
        self.terminating.set()
        self.stop()

    # ── the worker thread ──────────────────────────────────────────────────

    def work(self):
        if self.s is None:
            self.set_state("error", "fix the settings and run the app again")
            return
        try:
            self.load()
            if self.s.dry_run:
                self.dry_run()
                return
        except Exception as exc:  # noqa: BLE001 - keep serving the page with the error
            log.error("%s", traceback.format_exc())
            with self.lock:
                self.error = f"{type(exc).__name__}: {exc}"
            self.set_state("error", "see the log: wendy device logs")
            return
        self.set_state("ready", "the arm is limp; press Start to run the policy")
        if self.s.autostart and not self.terminating.is_set():
            self.start_requested.set()
        while not self.terminating.is_set():
            if self.start_requested.wait(0.2):
                self.start_requested.clear()
                if not self.terminating.is_set():
                    self.run_once()

    def load(self):
        s = self.s
        self.set_state("loading", "importing LeRobot")
        lr = self.lr = LeRobot()
        common.load_calibration(s.follower_calibration, "follower", "SO101_FOLLOWER_CALIBRATION")

        commit, how = None, "local directory"
        if not s.model_is_local:
            self.set_state("loading", f"resolving {s.model_path}@{s.model_revision or 'main'}")
            commit, how = policy_settings.resolve_revision(s.model_path, s.model_revision, hub_commit, cached_commit)
        # The repo and the exact commit this app runs, for the log and the page.
        log.info("model %s, revision %s", s.model_path, how)
        with self.lock:
            self.model.update(path=s.model_path, requested=s.model_revision or "main", commit=commit, revision=how)

        self.set_state("loading", "reading the model config")
        cfg = lr.PreTrainedConfig.from_pretrained(s.model_path, revision=commit)
        policy_type = policy_settings.check_policy_type(cfg.type, s.model_path)
        model_cameras = policy_settings.check_model_features(
            feature_table(cfg.input_features), feature_table(cfg.output_features), s.model_path
        )
        cameras = policy_settings.match_cameras(s.cameras, model_cameras)
        sizes = {name: policy_settings.camera_size(s, model_cameras[name]) for name in cameras}
        device = policy_settings.choose_device(s.device, lr.torch.cuda.is_available())
        cfg.device = device
        cfg.pretrained_path = s.model_path
        cfg.pretrained_revision = commit
        with self.lock:
            self.model.update(
                type=policy_type,
                device=device,
                cameras={name: f"{cameras[name]} at {w}x{h}" for name, (w, h) in sizes.items()},
                lerobot=lr.version,
            )

        self.set_state("loading", f"downloading and loading the {policy_type} model")
        policy = lr.get_policy_class(policy_type).from_pretrained(s.model_path, config=cfg, revision=commit)
        policy.to(device)
        policy.eval()
        preprocessor, postprocessor = lr.make_pre_post_processors(
            policy_cfg=cfg,
            pretrained_path=s.model_path,
            pretrained_revision=commit,
            # As lerobot-rollout does: run the preprocessor on this device, with no renaming.
            preprocessor_overrides={
                "device_processor": {"device": device},
                "rename_observations_processor": {"rename_map": {}},
            },
        )
        self.loaded = LoadedPolicy(policy, preprocessor, postprocessor, policy_type, device, cameras, sizes)
        log.info("loaded %s policy %s (%s) on %s; cameras %s", policy_type, s.model_path, how, device, cameras)

    def make_follower(self):
        s, lr, loaded = self.s, self.lr, self.loaded
        cameras = {
            name: lr.OpenCVCameraConfig(
                index_or_path=common.camera_index_or_path(device),
                fps=s.fps,
                width=loaded.camera_sizes[name][0],
                height=loaded.camera_sizes[name][1],
                fourcc=s.camera_fourcc,
            )
            for name, device in loaded.cameras.items()
        }
        # LeRobot looks for <calibration_dir>/<id>.json, so the file name is the arm's id.
        return lr.Follower(
            lr.SO101FollowerConfig(
                port=s.follower_port,
                id=s.follower_calibration.stem,
                calibration_dir=s.follower_calibration.parent,
                cameras=cameras,
                use_degrees=s.use_degrees,
                # LeRobot caps how far one command may move a joint from where it is.
                max_relative_target=s.max_relative_target,
            )
        )

    def policy_features(self, robot):
        """Feature names for build_dataset_frame/make_robot_action, as lerobot-rollout derives them."""
        lr = self.lr
        return lr.combine_feature_dicts(
            lr.hw_to_dataset_features(robot.action_features, lr.ACTION),
            lr.hw_to_dataset_features(robot.observation_features, lr.OBS_STR, use_video=True),
        )

    def run_once(self):
        s = self.s
        robot = None
        rest_pose = None
        with self.lock:
            self.stats = {"steps": 0, "hz": None, "inference_ms": None}
        self.set_state("starting", f"connecting the follower on {s.follower_port}")
        try:
            robot = self.make_follower()
            robot.connect()
            # Where the arm was when the run started; Stop brings it back here.
            rest_pose = common.present_pose(robot)
            if not self.stop_requested.is_set():
                self.set_state("running", f"{self.loaded.policy_type} policy on {self.loaded.device}")
                self.control_loop(robot)
        except Exception as exc:  # noqa: BLE001 - stop safely and show the error
            log.error("%s", traceback.format_exc())
            with self.lock:
                self.error = f"{type(exc).__name__}: {exc}"
        finally:
            if robot is None:
                report = "the arm was not connected"
            else:
                self.set_state("stopping", "returning the arm to its start pose, then torque off")
                report = common.safe_stop(robot, rest_pose, s.return_seconds, fps=s.fps)
            log.info("stopped: %s", report)
            self.set_state("ready", f"stopped: {report}")

    def control_loop(self, robot):
        s, lr, loaded = self.s, self.lr, self.loaded
        _teleop_proc, robot_proc, obs_proc = lr.make_default_processors()
        features = self.policy_features(robot)
        torch_device = lr.torch.device(loaded.device)
        loaded.policy.reset()
        loaded.preprocessor.reset()
        loaded.postprocessor.reset()
        period = 1.0 / s.fps
        started = window_start = time.perf_counter()
        steps = window_steps = 0
        while not self.stop_requested.is_set():
            tick = time.perf_counter()
            if s.run_seconds and tick - started >= s.run_seconds:
                log.info("SO101_RUN_SECONDS=%s reached", s.run_seconds)
                break
            observation = robot.get_observation()
            frame = lr.build_dataset_frame(features, obs_proc(observation), prefix=lr.OBS_STR)
            inference_start = time.perf_counter()
            action = lr.predict_action(
                frame,
                loaded.policy,
                torch_device,
                loaded.preprocessor,
                loaded.postprocessor,
                loaded.policy.config.use_amp,
                task=s.task,
                robot_type=robot.robot_type,
            )
            inference_ms = (time.perf_counter() - inference_start) * 1000
            action_dict = lr.make_robot_action(action, features)
            robot.send_action(robot_proc((action_dict, observation)))
            steps += 1
            window_steps += 1
            now = time.perf_counter()
            if now - window_start >= 1.0:
                with self.lock:
                    self.stats = {
                        "steps": steps,
                        "hz": round(window_steps / (now - window_start), 1),
                        "inference_ms": round(inference_ms, 1),
                    }
                window_start, window_steps = now, 0
            lr.precise_sleep(max(0.0, period - (time.perf_counter() - tick)))

    # ── dry run: check everything without moving anything ──────────────────

    def dry_run(self):
        s, lr, loaded = self.s, self.lr, self.loaded
        report = []

        def add(line):
            report.append(line)
            log.info("dry run: %s", line)
            with self.lock:
                self.report = list(report)

        self.set_state("checking", "dry run: the arm is never connected, so nothing moves")
        add(f"LeRobot {lr.version}; {loaded.policy_type} policy {s.model_path} ({self.model['revision']})")
        add(f"device {loaded.device}; cameras {self.model['cameras']}")
        add(self.synthetic_inference())
        add(common.probe_arm(s.follower_port, common.load_calibration(
            s.follower_calibration, "follower", "SO101_FOLLOWER_CALIBRATION")))
        for name, device in loaded.cameras.items():
            width, height = loaded.camera_sizes[name]
            add(common.probe_camera(name, device, width, height, s.fps, s.camera_fourcc))
        self.set_state("dry-run done", "set SO101_DRY_RUN=false to run the policy")

    def synthetic_inference(self):
        """Run the policy on a gray image and a zero pose to time inference on this device."""
        import numpy as np

        lr, loaded, s = self.lr, self.loaded, self.s
        robot = self.make_follower()  # built, not connected: it only supplies the feature names
        features = self.policy_features(robot)
        observation = {f"{joint}.pos": 0.0 for joint in common.JOINTS}
        for name, (width, height) in loaded.camera_sizes.items():
            observation[name] = np.full((height, width, 3), 128, dtype=np.uint8)
        frame = lr.build_dataset_frame(features, observation, prefix=lr.OBS_STR)
        timings = []
        try:
            for _ in range(DRY_RUN_PASSES + 1):
                # reset() empties the action queue, so every pass runs the full model.
                loaded.policy.reset()
                started = time.perf_counter()
                action = lr.predict_action(
                    frame, loaded.policy, lr.torch.device(loaded.device), loaded.preprocessor,
                    loaded.postprocessor, loaded.policy.config.use_amp, task=s.task, robot_type=robot.robot_type,
                )
                timings.append((time.perf_counter() - started) * 1000)
            values = lr.make_robot_action(action, features)
        except Exception as exc:  # noqa: BLE001
            log.error("%s", traceback.format_exc())
            return f"synthetic inference FAILED: {type(exc).__name__}: {common.first_line(exc)}"
        joints = ", ".join(f"{key.removesuffix('.pos')}={value:.1f}" for key, value in values.items())
        passes = ", ".join(f"{ms:.0f}" for ms in timings[1:])
        return f"synthetic inference: one full pass takes {passes} ms (after a warm-up pass); action {joints}"


PAGE = """<!doctype html><html><head><meta charset=utf-8><title>SO-101 policy</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{font-family:ui-monospace,Menlo,monospace;background:#0c0c0c;color:#e8e8e8;margin:20px;max-width:900px}
button{font-size:16px;padding:10px 16px;margin:6px 6px 6px 0}.muted{color:#888}.err{color:#f66;white-space:pre-wrap}
.state{font-size:22px;margin:10px 0}.running{color:#f55}.ready{color:#7f7}.loading,.starting,.stopping{color:#fc5}
pre{background:#000;border:1px solid #333;padding:10px;white-space:pre-wrap}</style></head><body>
<h2>SO-101 policy</h2>
<div class=muted>Start hands the follower arm to the policy. Stop returns it to where it started, then turns torque off.
Clear the workspace before you press Start.</div>
<div id=state class=state>...</div>
<div>
<button id=start onclick="send('start')">Start</button>
<button id=stop onclick="send('stop')">Stop</button>
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
  st.className = 'state ' + d.state;
  st.textContent = d.state + (d.detail ? ': ' + d.detail : '');
  document.getElementById('start').disabled = !d.can_start;
  document.getElementById('stop').disabled = !d.can_stop;
  if (d.error) document.getElementById('err').textContent = d.error;
  const m = d.model || {};
  const lines = ['model: ' + (m.path || '?'), 'revision: ' + (m.revision || '?'),
    'policy: ' + (m.type || '?') + ' on ' + (m.device || '?'), 'cameras: ' + JSON.stringify(m.cameras || {}),
    'follower: ' + d.follower_port + ', ' + d.fps + ' Hz target', 'task: ' + d.task];
  if (d.stats && d.stats.hz !== null) lines.push('running at ' + d.stats.hz + ' Hz, last policy call ' +
    d.stats.inference_ms + ' ms, ' + d.stats.steps + ' steps');
  document.getElementById('info').textContent = lines.concat(d.report).join('\\n');
}
setInterval(poll, 500); poll();
</script></body></html>"""


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings, error = None, None
    try:
        settings = policy_settings.load(os.environ)
    except common.SettingsError as exc:
        error = exc
        log.error("settings: %s", exc)
    runner = Runner(settings, error)
    port = settings.http_port if settings else int(os.environ.get("SO101_HTTP_PORT") or 8093)
    common.ControlServer(port, PAGE, runner.status, {"start": runner.start, "stop": runner.stop}).start()
    log.info("SO-101 policy page on http://0.0.0.0:%d", port)
    if settings:
        log.info(
            "model %s, requested revision %s; follower %s; cameras %s",
            settings.model_path,
            settings.model_revision or "main",
            settings.follower_port,
            settings.cameras,
        )

    stopping = threading.Event()

    def on_signal(signum, _frame):
        if stopping.is_set():
            log.warning("second signal: exiting without stopping the arm")
            os._exit(1)
        log.info("signal %d: stopping", signum)
        stopping.set()
        runner.terminate()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    worker = threading.Thread(target=runner.work, name="policy", daemon=True)
    worker.start()
    while not stopping.wait(1.0):
        pass
    # Wendy sends SIGKILL 10 s after SIGTERM; the safe stop takes at most
    # SO101_RETURN_SECONDS (8 s or less) plus releasing the bus.
    worker.join(timeout=9.5)
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
