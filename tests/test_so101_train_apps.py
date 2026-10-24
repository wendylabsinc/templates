"""Check the so101-record and so101-policy templates without LeRobot or hardware.

The apps import LeRobot only inside functions, so their modules load here with
the standard library. Hardware is replaced by a fake servo bus.
"""

import importlib
import json
from pathlib import Path
import re
import sys

import pytest

PYTHON = Path(__file__).resolve().parents[1] / "python"
RECORD = PYTHON / "so101-record"
POLICY = PYTHON / "so101-policy"
SHARED = ("so101_common",)


def load_modules(template, *names):
    """Import modules from one template directory, with that template's so101_common."""
    owned = SHARED + names
    saved = {name: sys.modules.pop(name) for name in owned if name in sys.modules}
    sys.path.insert(0, str(template))
    try:
        modules = [importlib.import_module(name) for name in names]
        common = sys.modules["so101_common"]
    finally:
        sys.path.remove(str(template))
        for name in owned:
            sys.modules.pop(name, None)
        sys.modules.update(saved)
    return (common, *modules)


rcommon, record_settings, record = load_modules(RECORD, "record_settings", "record")
pcommon, policy_settings, run_policy = load_modules(POLICY, "policy_settings", "run_policy")
# Each template loads its own copy of so101_common, so each has its own class.
SettingsError = (rcommon.SettingsError, pcommon.SettingsError)
COMMIT = "4cbf28905b71f874a9e10123b01099c0e758bfcf"
OTHER_COMMIT = "0123456789abcdef0123456789abcdef01234567"


# ── fakes for LeRobot's servo bus and SO follower ────────────────────────────


class FakeBus:
    """Records what a LeRobot FeetechMotorsBus would be asked to do."""

    def __init__(self, positions=None, fail_goals=False):
        self.present = dict(positions or {joint: 0.0 for joint in rcommon.JOINTS})
        self.torque = True
        self.is_connected = True
        self.fail_goals = fail_goals
        self.calls = []

    def sync_read(self, name, num_retry=0):
        assert name == "Present_Position"
        self.calls.append(("read", name))
        return dict(self.present)

    def sync_write(self, name, values, num_retry=0):
        assert name == "Goal_Position"
        if self.fail_goals:
            raise ConnectionError("servo 4 did not answer")
        self.calls.append(("goal", dict(values)))
        self.present.update(values)  # the servos reach every goal

    @property
    def goals(self):
        return [call[1] for call in self.calls if call[0] == "goal"]

    def disable_torque(self):
        self.calls.append(("torque", False))
        self.torque = False

    def write_calibration(self, calibration):
        self.calls.append(("calibration", calibration))

    def disconnect(self, disable_torque=True):
        self.calls.append(("disconnect", disable_torque))
        if disable_torque:
            self.torque = False
        self.is_connected = False


class FakeCamera:
    def __init__(self, connected=True, fail=False):
        self.is_connected = connected
        self.fail = fail

    def disconnect(self):
        if self.fail:
            raise OSError("camera unplugged")
        self.is_connected = False


class FakeArm:
    """Duck-types LeRobot's SOFollower for the stop sequence."""

    def __init__(self, positions=None, fail_moves=False, cameras=None):
        self.bus = FakeBus(positions, fail_goals=fail_moves)
        self.cameras = cameras or {}
        self.sent = []

    @property
    def is_connected(self):
        return self.bus.is_connected and all(camera.is_connected for camera in self.cameras.values())

    def send_action(self, action):
        if not self.is_connected:  # LeRobot's @check_if_not_connected, cameras included
            raise RuntimeError("not connected")
        self.sent.append(dict(action))
        self.bus.sync_write("Goal_Position", {key.removesuffix(".pos"): value for key, value in action.items()})
        return action

    def disconnect(self):
        if not self.is_connected:  # LeRobot's @check_if_not_connected
            raise RuntimeError("not connected")
        self.bus.disconnect(True)
        for camera in self.cameras.values():
            camera.disconnect()


class Sleeps(list):
    def __call__(self, seconds):
        self.append(seconds)


# ── shared module ────────────────────────────────────────────────────────────


def test_shared_module_copies_are_identical():
    assert (RECORD / "so101_common.py").read_bytes() == (POLICY / "so101_common.py").read_bytes()


@pytest.mark.parametrize("raw, expected", [("", True), ("yes", True), ("ON", True), ("0", False), ("off", False)])
def test_env_bool(raw, expected):
    assert rcommon.env_bool({"X": raw}, "X", True) is expected


@pytest.mark.parametrize("raw", ["maybe", "2"])
def test_env_bool_rejects_other_words(raw):
    with pytest.raises(SettingsError, match="yes/no"):
        rcommon.env_bool({"X": raw}, "X", True)


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "ten"])
def test_env_float_rejects_non_finite(raw):
    with pytest.raises(SettingsError):
        rcommon.env_float({"X": raw}, "X", 1.0)


def test_parse_cameras():
    assert rcommon.parse_cameras("front=/dev/video0, wrist=/dev/video2", "front") == {
        "front": "/dev/video0",
        "wrist": "/dev/video2",
    }
    assert rcommon.parse_cameras("/dev/video4", "front") == {"front": "/dev/video4"}
    assert rcommon.parse_cameras("/dev/video4", None) == {None: "/dev/video4"}
    assert rcommon.parse_cameras("top=2", "front") == {"top": "2"}


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "empty"),
        ("Front=/dev/video0", "lowercase"),
        ("front=/dev/video0,front=/dev/video2", "twice"),
        ("front=", "no device"),
        ("front=video0", "device path"),
    ],
)
def test_parse_cameras_rejects(text, message):
    with pytest.raises(SettingsError, match=message):
        rcommon.parse_cameras(text, "front")


def calibration(**changes):
    data = {
        joint: {"id": index + 1, "drive_mode": 0, "homing_offset": 0, "range_min": 1000, "range_max": 3000}
        for index, joint in enumerate(rcommon.JOINTS)
    }
    for joint, fields in changes.items():
        data[joint].update(fields)
    return data


def test_calibration_accepts_lerobot_shape_and_warns_on_odd_ranges():
    assert rcommon.check_calibration(calibration(), "f") == []
    warnings = rcommon.check_calibration(calibration(wrist_roll={"range_min": -20}), "f")
    assert len(warnings) == 1 and "wrist_roll" in warnings[0]


@pytest.mark.parametrize(
    "data, message",
    [
        ({"shoulder_pan": {}}, "no entry for"),
        (calibration(gripper={"id": 7}), "servo id 7"),
        (calibration(elbow_flex={"range_min": 3000}), "not below range_max"),
        (calibration(wrist_flex={"drive_mode": 2}), "drive_mode"),
        (calibration(shoulder_lift={"homing_offset": True}), "whole number"),
        ([], "JSON object"),
    ],
)
def test_calibration_rejects(data, message):
    with pytest.raises(SettingsError, match=message):
        rcommon.check_calibration(data, "f")


def test_missing_calibration_names_the_files_that_are_there(tmp_path):
    (tmp_path / "leader.json").write_text(json.dumps(calibration()))
    with pytest.raises(SettingsError, match="holds: leader.json"):
        rcommon.load_calibration(tmp_path / "follower.json", "follower", "SO101_FOLLOWER_CALIBRATION")
    loaded = rcommon.load_calibration(tmp_path / "leader.json", "leader", "SO101_LEADER_CALIBRATION")
    assert loaded.data["gripper"]["id"] == 6


def test_cross_site_posts_are_refused():
    assert rcommon.same_origin({"Host": "robot:8092"})
    assert rcommon.same_origin({"Host": "robot:8092", "Origin": "http://robot:8092"})
    assert not rcommon.same_origin({"Host": "robot:8092", "Origin": "https://evil.example"})
    assert not rcommon.same_origin({"Host": "robot:8092", "Sec-Fetch-Site": "cross-site"})


# ── the stop sequence, against a fake servo bus ──────────────────────────────


def test_move_smoothly_interpolates_from_the_present_pose():
    arm = FakeArm({joint: 0.0 for joint in rcommon.JOINTS})
    sleeps = Sleeps()
    target = {"shoulder_pan.pos": 30.0, "gripper.pos": 50.0, "not_a_joint.pos": 1.0}
    sent = rcommon.move_smoothly(arm, target, seconds=1.0, fps=10, sleep=sleeps)
    assert sent == 10 and len(arm.bus.goals) == 10 and sleeps == [0.1] * 10
    assert arm.bus.goals[0] == pytest.approx({"shoulder_pan": 3.0, "gripper": 5.0})
    assert arm.bus.goals[-1] == {"shoulder_pan": 30.0, "gripper": 50.0}
    # It reads and writes the servo bus itself, so a failed camera cannot block it.
    assert arm.bus.calls[0] == ("read", "Present_Position") and arm.sent == []


def test_move_smoothly_works_after_a_camera_failed():
    arm = FakeArm(cameras={"wrist": FakeCamera(connected=False)})
    assert not arm.is_connected
    assert rcommon.move_smoothly(arm, {"gripper.pos": 9.0}, 0.2, fps=10, sleep=Sleeps()) == 2
    assert arm.bus.present["gripper"] == 9.0


def test_move_smoothly_can_be_cut_short_or_skipped():
    arm = FakeArm()
    calls = iter([False, False, True])
    assert rcommon.move_smoothly(arm, {"gripper.pos": 9.0}, 1.0, fps=10, sleep=Sleeps(),
                                 should_stop=lambda: next(calls)) == 2
    assert rcommon.move_smoothly(arm, {"gripper.pos": 9.0}, 0, sleep=Sleeps()) == 0
    assert rcommon.move_smoothly(arm, {}, 1.0, sleep=Sleeps()) == 0


def test_safe_stop_returns_to_the_start_pose_then_turns_torque_off():
    rest = {f"{joint}.pos": 0.0 for joint in rcommon.JOINTS}
    arm = FakeArm({joint: 40.0 for joint in rcommon.JOINTS}, cameras={"wrist": FakeCamera()})
    report = rcommon.safe_stop(arm, rest, seconds=2.0, fps=5, sleep=Sleeps())
    assert len(arm.bus.goals) == 10 and arm.bus.goals[-1] == {joint: 0.0 for joint in rcommon.JOINTS}
    assert arm.bus.present == {joint: 0.0 for joint in rcommon.JOINTS}
    assert arm.bus.calls[-1] == ("disconnect", True)
    assert not arm.bus.torque and not arm.bus.is_connected
    assert not arm.cameras["wrist"].is_connected
    assert report == "arm returned to its start pose; torque off"


def test_safe_stop_turns_torque_off_even_when_the_move_fails():
    arm = FakeArm(fail_moves=True)
    report = rcommon.safe_stop(arm, {"gripper.pos": 0.0}, seconds=1.0, sleep=Sleeps())
    assert "could not return to its start pose (servo 4 did not answer)" in report
    assert report.endswith("torque off")
    assert not arm.bus.torque and not arm.bus.is_connected


def test_safe_stop_without_a_start_pose_only_releases():
    arm = FakeArm()
    report = rcommon.safe_stop(arm, None, seconds=3.0, sleep=Sleeps())
    assert arm.bus.goals == [] and report == "arm not moved; torque off"
    assert not arm.bus.torque


def test_release_falls_back_to_the_bus_when_disconnect_fails():
    arm = FakeArm()

    def broken_disconnect():
        raise OSError("port busy")

    arm.disconnect = broken_disconnect
    assert rcommon.release(arm) is True
    assert arm.bus.calls[-1] == ("disconnect", True) and not arm.bus.torque


def test_release_after_a_half_finished_connect():
    # The bus opened but the camera did not: LeRobot's is_connected is False,
    # so its disconnect() would refuse; the bus must still be closed with torque off.
    arm = FakeArm(cameras={"front": FakeCamera(connected=False)})
    assert not arm.is_connected
    assert rcommon.release(arm) is True
    assert arm.bus.calls == [("disconnect", True)]


def test_release_reports_torque_off_when_only_the_camera_fails():
    arm = FakeArm(cameras={"front": FakeCamera(fail=True)})
    assert rcommon.release(arm) is True
    assert not arm.bus.torque


def test_release_of_a_closed_arm_does_nothing():
    arm = FakeArm()
    arm.bus.is_connected = False
    assert rcommon.release(arm) is True
    assert arm.bus.calls == []


class FakeLeRobotArm:
    """Stands in for LeRobot's SOFollower/SOLeader: calibrate() would prompt on stdin."""

    def __init__(self, calibration_data):
        self.bus = FakeBus({joint: 100.0 + index for index, joint in enumerate(rcommon.JOINTS)})
        self.calibration = calibration_data
        self.calibration_fpath = Path("/calibration/follower.json")
        self.id = "follower"

    def calibrate(self):
        raise AssertionError("LeRobot's calibrate() waits for terminal input")

    def configure(self):
        self.bus.calls.append(("configure",))


def test_without_prompts_writes_the_file_with_torque_off():
    arm = rcommon.without_prompts(FakeLeRobotArm, hold_on_connect=True)({"gripper": "cal"})
    arm.calibrate()
    assert arm.bus.calls == [("torque", False), ("calibration", {"gripper": "cal"})]
    assert type(arm).__name__ == "FakeLeRobotArmNoPrompt"


def test_without_prompts_refuses_to_calibrate_without_a_file():
    arm = rcommon.without_prompts(FakeLeRobotArm, hold_on_connect=True)({})
    with pytest.raises(SettingsError, match="no calibration file"):
        arm.calibrate()
    assert arm.bus.calls == []


def test_follower_holds_its_present_position_before_torque_goes_on():
    arm = rcommon.without_prompts(FakeLeRobotArm, hold_on_connect=True)({})
    arm.configure()
    present = {joint: 100.0 + index for index, joint in enumerate(rcommon.JOINTS)}
    assert arm.bus.calls == [("read", "Present_Position"), ("goal", present), ("configure",)]


def test_leader_configure_is_unchanged():
    arm = rcommon.without_prompts(FakeLeRobotArm, hold_on_connect=False)({})
    arm.configure()
    assert arm.bus.calls == [("configure",)]


# ── recorder settings ────────────────────────────────────────────────────────


def test_recorder_defaults():
    s = record_settings.load({})
    assert s.repo_id == "local/so101_demos"
    assert s.dataset_root == Path("/data/lerobot/local/so101_demos")
    assert s.push is False and "local dataset only" in s.push_reason
    assert s.cameras == {"front": "/dev/video0"} and s.camera_fourcc == "MJPG"
    assert (s.fps, s.num_episodes, s.episode_seconds, s.reset_seconds) == (30, 50, 30.0, 10.0)
    assert (s.follower_port, s.leader_port) == ("/dev/ttyACM0", "/dev/ttyACM1")
    assert s.task == rcommon.DEFAULT_TASK
    assert s.private and s.use_degrees and s.streaming_encoding and not s.dry_run
    assert s.video_codec == "libsvtav1"


@pytest.mark.parametrize(
    "mode, repo_id, token, push",
    [
        ("auto", "team/demos", True, True),
        ("auto", "team/demos", False, False),
        ("auto", "local/demos", True, False),
        ("true", "team/demos", True, True),
        ("false", "team/demos", True, False),
        ("false", "local/demos", False, False),
    ],
)
def test_push_decision(mode, repo_id, token, push):
    decided, reason = record_settings.push_decision(mode, repo_id, token)
    assert decided is push and reason
    if push:
        assert f"https://huggingface.co/datasets/{repo_id}" in reason


@pytest.mark.parametrize(
    "repo_id, token, message",
    [("local/demos", True, "not a local/ name"), ("team/demos", False, "needs HF_TOKEN")],
)
def test_push_true_needs_a_hub_repo_and_a_token(repo_id, token, message):
    with pytest.raises(SettingsError, match=message):
        record_settings.push_decision("true", repo_id, token)


def test_recorder_upload_settings_from_the_environment():
    s = record_settings.load({"SO101_DATASET_REPO_ID": "team/demos", "HF_TOKEN": " hf_x "})
    assert s.push and s.dataset_root == Path("/data/lerobot/team/demos")
    assert not record_settings.load({"SO101_DATASET_REPO_ID": "team/demos", "HF_TOKEN": "  "}).push


@pytest.mark.parametrize(
    "env, message",
    [
        ({"SO101_PUSH_TO_HUB": "maybe"}, "auto, true or false"),
        ({"SO101_DATASET_REPO_ID": "demos"}, "not a Hugging Face repo id"),
        ({"SO101_DATASET_REPO_ID": "team/eval_demos"}, "reserved"),
        ({"SO101_DATASET_REPO_ID": "team/de..mos"}, "not a Hugging Face repo id"),
        ({"SO101_LEADER_PORT": "/dev/ttyACM0"}, "both /dev/ttyACM0"),
        ({"SO101_FOLLOWER_PORT": "ttyACM0"}, "device path"),
        ({"SO101_LEADER_CALIBRATION": "/calibration/follower.json"}, "its own calibration"),
        ({"SO101_VIDEO_CODEC": "vp9"}, "must be one of"),
        ({"SO101_CAMERA_FOURCC": "MJPEG"}, "4 characters"),
        ({"SO101_FPS": "0"}, "below the minimum"),
        ({"SO101_EPISODE_SECONDS": "0.5"}, "below the minimum"),
        ({"SO101_NUM_EPISODES": "many"}, "whole number"),
        ({"SO101_TASK": "   "}, "SO101_TASK is empty"),
    ],
)
def test_recorder_rejects(env, message):
    with pytest.raises(SettingsError, match=message):
        record_settings.load(env)


def test_camera_fourcc_auto_keeps_the_camera_default():
    assert record_settings.load({"SO101_CAMERA_FOURCC": "auto"}).camera_fourcc is None


def write_info(root, episodes):
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text(json.dumps({"codebase_version": "v3.0", "total_episodes": episodes}))


def test_dataset_state(tmp_path):
    assert record_settings.dataset_state(tmp_path / "missing") == "new"
    (tmp_path / "empty-dir").mkdir()
    assert record_settings.dataset_state(tmp_path / "empty-dir") == "new"
    write_info(tmp_path / "no-episodes", 0)
    assert record_settings.dataset_state(tmp_path / "no-episodes") == "empty"
    write_info(tmp_path / "recorded", 12)
    assert record_settings.dataset_state(tmp_path / "recorded") == "resume"
    assert record.count_episodes(tmp_path / "recorded") == 12
    assert record.count_episodes(tmp_path / "missing") == 0
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "notes.txt").write_text("mine")
    assert record_settings.dataset_state(tmp_path / "other") == "foreign"
    (tmp_path / "broken" / "meta").mkdir(parents=True)
    (tmp_path / "broken" / "meta" / "info.json").write_text("{")
    assert record_settings.dataset_state(tmp_path / "broken") == "foreign"


def test_recorder_buttons_act_only_during_an_episode_or_reset():
    recorder = record.Recorder(record_settings.load({}))
    with pytest.raises(SettingsError, match="nothing to control"):
        recorder.next()
    recorder.state = "recording"
    recorder.rerecord()
    assert recorder.events == {"exit_early": True, "rerecord_episode": True, "stop_recording": False}
    recorder.state = "saving"
    recorder.stop()  # Stop works in any phase
    assert recorder.events["stop_recording"]


def test_recorder_upload_button_needs_a_finished_session_and_a_token():
    with pytest.raises(SettingsError, match="local dataset only"):
        record.Recorder(record_settings.load({})).push()
    recorder = record.Recorder(record_settings.load({"SO101_DATASET_REPO_ID": "team/d", "HF_TOKEN": "x"}))
    recorder.state = "recording"
    with pytest.raises(SettingsError, match="once the session has finished"):
        recorder.push()


# ── policy settings ──────────────────────────────────────────────────────────


def no_dirs(_path):
    return False


def test_policy_defaults():
    s = policy_settings.load({}, is_dir=no_dirs)
    assert s.model_path == policy_settings.DEFAULT_MODEL and s.model_revision is None and not s.model_is_local
    assert s.cameras == {None: "/dev/video0"}
    assert (s.camera_width, s.camera_height, s.camera_fourcc) == (None, None, "MJPG")
    assert (s.fps, s.device, s.max_relative_target, s.return_seconds, s.run_seconds) == (30, "auto", 10.0, 3.0, 0.0)
    assert not s.autostart and not s.dry_run and s.use_degrees
    assert s.http_port == 8093 and s.task == rcommon.DEFAULT_TASK


def test_model_source():
    def dirs(path):
        return path in ("/models/act", "checkpoints/act")

    assert policy_settings.model_source("/models/act", None, is_dir=dirs) is True
    assert policy_settings.model_source("checkpoints/act", None, is_dir=dirs) is True
    assert policy_settings.model_source("team/so101-act", "v2", is_dir=dirs) is False
    with pytest.raises(SettingsError, match="applies to Hub models only"):
        policy_settings.model_source("/models/act", "main", is_dir=dirs)
    with pytest.raises(SettingsError, match="not a directory in the container"):
        policy_settings.model_source("./missing", None, is_dir=dirs)
    for bad in ("act", "team/so101 act", "team/../x", "team/a--b"):
        with pytest.raises(SettingsError, match="neither a Hugging Face model id"):
            policy_settings.model_source(bad, None, is_dir=dirs)
    with pytest.raises(SettingsError, match="is empty"):
        policy_settings.model_source("", None, is_dir=dirs)


@pytest.mark.parametrize("revision", ["main", "v1.0", COMMIT, "refs/pr/3", "feature_x"])
def test_valid_revisions(revision):
    assert policy_settings.check_revision(revision) == revision


@pytest.mark.parametrize("revision", ["-main", "a b", "main..dev", "dev/", "/main", "x.lock"])
def test_invalid_revisions(revision):
    with pytest.raises(SettingsError, match="not a branch, tag or commit"):
        policy_settings.check_revision(revision)


def test_revision_from_the_environment():
    s = policy_settings.load({"SO101_MODEL_PATH": "team/act", "SO101_MODEL_REVISION": f" {COMMIT} "}, is_dir=no_dirs)
    assert s.model_revision == COMMIT
    assert policy_settings.load({"SO101_MODEL_REVISION": ""}, is_dir=no_dirs).model_revision is None


def lookup_returning(commit, calls=None):
    def lookup(repo_id, revision):
        if calls is not None:
            calls.append((repo_id, revision))
        return commit

    return lookup


def lookup_failing(message):
    def lookup(_repo_id, _revision):
        raise ConnectionError(message)

    return lookup


def test_resolve_main_and_branches_to_a_commit():
    calls = []
    assert policy_settings.resolve_revision("team/act", None, lookup_returning(COMMIT, calls)) == (
        COMMIT,
        f"main -> commit {COMMIT}",
    )
    assert calls == [("team/act", None)]
    assert policy_settings.resolve_revision("team/act", "v2", lookup_returning(COMMIT)) == (
        COMMIT,
        f"v2 -> commit {COMMIT}",
    )
    assert policy_settings.resolve_revision("team/act", COMMIT, lookup_returning(COMMIT)) == (
        COMMIT,
        f"commit {COMMIT}",
    )


def test_resolve_offline():
    # A pinned commit is used as is; a branch falls back to the local cache.
    commit, how = policy_settings.resolve_revision("team/act", COMMIT, lookup_failing("no route"))
    assert commit == COMMIT and "not checked on the Hub: no route" in how
    commit, how = policy_settings.resolve_revision(
        "team/act", None, lookup_failing("offline"), cached=lambda repo, rev: OTHER_COMMIT
    )
    assert commit == OTHER_COMMIT and "from the local cache" in how
    with pytest.raises(SettingsError, match="could not resolve team/act@main .*HF_TOKEN"):
        policy_settings.resolve_revision("team/act", None, lookup_failing("401"), cached=lambda r, v: None)


def test_resolve_rejects_odd_hub_answers():
    with pytest.raises(SettingsError, match="not a commit id"):
        policy_settings.resolve_revision("team/act", "main", lookup_returning("main"))
    with pytest.raises(SettingsError, match="resolved commit"):
        policy_settings.resolve_revision("team/act", COMMIT, lookup_returning(OTHER_COMMIT))


def test_commit_from_cache_path():
    path = f"/data/huggingface/hub/models--team--act/snapshots/{COMMIT}/config.json"
    assert policy_settings.commit_from_cache_path(path) == COMMIT
    assert policy_settings.commit_from_cache_path("/data/huggingface/hub/models--team--act/config.json") is None
    assert policy_settings.commit_from_cache_path(f"/x/snapshots/{COMMIT[:12]}/config.json") is None


@pytest.mark.parametrize("policy_type", ["act", "smolvla"])
def test_supported_policy_types(policy_type):
    assert policy_settings.check_policy_type(policy_type, "m") == policy_type


def test_other_policy_types_are_refused_with_a_hint():
    with pytest.raises(SettingsError, match="'diffusion' policy; this app runs act and smolvla"):
        policy_settings.check_policy_type("diffusion", "m")


ACT_INPUTS = {"observation.state": ("STATE", (6,)), "observation.images.wrist": ("VISUAL", (3, 480, 640))}
ACTION = {"action": ("ACTION", (6,))}


def test_model_features():
    assert policy_settings.check_model_features(ACT_INPUTS, ACTION, "m") == {"wrist": (480, 640)}
    smolvla = {
        **ACT_INPUTS,
        "observation.images.front": ("VISUAL", (3, 256, 320)),
        "observation.images.empty_camera_0": ("VISUAL", (3, 480, 640)),
    }
    assert policy_settings.check_model_features(smolvla, ACTION, "m") == {"wrist": (480, 640), "front": (256, 320)}


@pytest.mark.parametrize(
    "inputs, outputs, message",
    [
        ({**ACT_INPUTS, "observation.state": ("STATE", (14,))}, ACTION, "6-joint observation.state"),
        (ACT_INPUTS, {"action": ("ACTION", (7,))}, "6-joint action"),
        ({"observation.state": ("STATE", (6,))}, ACTION, "no camera image"),
        ({**ACT_INPUTS, "observation.environment_state": ("ENV", (10,))}, ACTION, "does not provide"),
        ({**ACT_INPUTS, "observation.images.depth": ("VISUAL", (1, 480, 640))}, ACTION, "color images"),
    ],
)
def test_model_features_rejected(inputs, outputs, message):
    with pytest.raises(SettingsError, match=message):
        policy_settings.check_model_features(inputs, outputs, "m")


def test_match_cameras():
    one = {"wrist": (480, 640)}
    two = {"wrist": (480, 640), "front": (480, 640)}
    assert policy_settings.match_cameras({None: "/dev/video0"}, one) == {"wrist": "/dev/video0"}
    assert policy_settings.match_cameras({"wrist": "/dev/video2"}, one) == {"wrist": "/dev/video2"}
    named = {"front": "/dev/video0", "wrist": "/dev/video2"}
    assert policy_settings.match_cameras(named, two) == named
    with pytest.raises(SettingsError, match="name every camera"):
        policy_settings.match_cameras({None: "/dev/video0"}, two)
    with pytest.raises(SettingsError, match="but the model expects wrist"):
        policy_settings.match_cameras({"front": "/dev/video0"}, one)


def test_camera_size_follows_the_model_unless_set():
    s = policy_settings.load({}, is_dir=no_dirs)
    assert policy_settings.camera_size(s, (480, 640)) == (640, 480)
    s = policy_settings.load({"SO101_CAMERA_WIDTH": "320", "SO101_CAMERA_HEIGHT": "240"}, is_dir=no_dirs)
    assert policy_settings.camera_size(s, (480, 640)) == (320, 240)


def test_choose_device():
    assert policy_settings.choose_device("auto", True) == "cuda"
    assert policy_settings.choose_device("auto", False) == "cpu"
    assert policy_settings.choose_device("cpu", True) == "cpu"
    with pytest.raises(SettingsError, match="sees no CUDA GPU"):
        policy_settings.choose_device("cuda", False)


@pytest.mark.parametrize(
    "env, message",
    [
        ({"SO101_CAMERA_WIDTH": "640"}, "or neither"),
        ({"SO101_POLICY_DEVICE": "mps"}, "auto, cpu, cuda"),
        ({"SO101_RETURN_SECONDS": "9"}, "above the maximum 8"),
        ({"SO101_MAX_RELATIVE_TARGET": "0"}, "below the minimum"),
        ({"SO101_MAX_RELATIVE_TARGET": "fast"}, "not a number"),
        ({"SO101_FOLLOWER_PORT": "COM3"}, "device path"),
        ({"SO101_TASK": " "}, "SO101_TASK is empty"),
        ({"SO101_MODEL_REVISION": "a b"}, "not a branch"),
    ],
)
def test_policy_rejects(env, message):
    with pytest.raises(SettingsError, match=message):
        policy_settings.load(env, is_dir=no_dirs)


def test_max_relative_target_can_be_turned_off():
    assert policy_settings.load({"SO101_MAX_RELATIVE_TARGET": "none"}, is_dir=no_dirs).max_relative_target is None
    assert policy_settings.load({"SO101_MAX_RELATIVE_TARGET": "4.5"}, is_dir=no_dirs).max_relative_target == 4.5


def test_policy_buttons():
    runner = run_policy.Runner(policy_settings.load({}, is_dir=no_dirs))
    with pytest.raises(SettingsError, match="cannot start while starting"):
        runner.start()
    runner.state = "ready"
    runner.start()
    assert runner.start_requested.is_set() and not runner.stop_requested.is_set()
    runner.stop()
    assert runner.stop_requested.is_set() and not runner.start_requested.is_set()
    runner.terminate()
    assert runner.terminating.is_set()


class ConnectableArm(FakeArm):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bus.is_connected = False
        self.bus.torque = False

    def connect(self):
        self.bus.is_connected = True
        self.bus.torque = True


def runner_with(arm, env=None):
    settings = policy_settings.load({"SO101_RETURN_SECONDS": "0.1", **(env or {})}, is_dir=no_dirs)
    runner = run_policy.Runner(settings)
    runner.loaded = run_policy.LoadedPolicy(None, None, None, "act", "cpu", {}, {})
    runner.make_follower = lambda: arm
    return runner


def test_a_failing_policy_step_still_returns_the_arm_and_turns_torque_off():
    start = {joint: 10.0 for joint in rcommon.JOINTS}
    arm = ConnectableArm(positions=start)
    runner = runner_with(arm)

    def policy_moves_then_fails(robot):
        robot.send_action({"shoulder_pan.pos": 80.0, "gripper.pos": 90.0})
        raise ConnectionError("Incorrect status packet!")

    runner.control_loop = policy_moves_then_fails
    runner.run_once()
    assert arm.bus.goals[0] == {"shoulder_pan": 80.0, "gripper": 90.0}
    assert arm.bus.goals[-1] == start
    assert not arm.bus.torque and not arm.bus.is_connected
    assert runner.state == "ready" and "torque off" in runner.detail
    assert runner.error == "ConnectionError: Incorrect status packet!"


def test_stop_before_the_policy_runs_only_releases_the_arm():
    arm = ConnectableArm()
    runner = runner_with(arm)
    runner.stop()

    def must_not_run(_robot):
        raise AssertionError("the policy ran after Stop")

    runner.control_loop = must_not_run
    runner.run_once()
    assert not arm.bus.torque and runner.state == "ready" and runner.error is None


def test_a_follower_that_cannot_be_built_leaves_the_runner_ready():
    runner = runner_with(None)

    def broken():
        raise ValueError("bad calibration file")

    runner.make_follower = broken
    runner.run_once()
    assert runner.state == "ready" and runner.detail == "stopped: the arm was not connected"
    assert runner.error == "ValueError: bad calibration file"


class FakeFeature:
    def __init__(self, kind, shape):
        self.type = type("FeatureType", (), {"value": kind})()
        self.shape = shape


def test_feature_table_reads_lerobot_policy_features():
    table = run_policy.feature_table({"observation.images.wrist": FakeFeature("VISUAL", [3, 480, 640])})
    assert table == {"observation.images.wrist": ("VISUAL", (3, 480, 640))}


# ── template files ───────────────────────────────────────────────────────────

ACTION_RE = re.compile(r"{{(.*?)}}", re.DOTALL)


def manifest(template):
    return json.loads((template / "template.json").read_text())


def defaults(template):
    return {variable["name"]: variable.get("default", "my-app") for variable in manifest(template)["variables"]}


def render(text, values):
    def action(match):
        name = re.fullmatch(r"\.([A-Z][A-Z0-9_]*)", match.group(1))
        assert name, f"unsupported template action {match.group(0)}"
        return str(values[name.group(1)])

    return ACTION_RE.sub(action, text)


def template_files(template):
    return sorted(p for p in template.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def dockerfile_env(template):
    """The ENV defaults of the rendered Dockerfile, as a dict."""
    text = render((template / "Dockerfile").read_text(), defaults(template))
    env = {}
    for statement in re.sub(r"\\\n", " ", text).splitlines():
        if statement.startswith("ENV "):
            for pair in statement[4:].split():
                key, _, value = pair.partition("=")
                env[key] = value
    return env


def env_names_read_by(*sources):
    names = set()
    for source in sources:
        names.update(re.findall(r'"((?:SO101|HF)_[A-Z_]+)"', source.read_text()))
    return names


@pytest.mark.parametrize("template", [RECORD, POLICY], ids=["record", "policy"])
def test_template_actions_are_declared_and_python_has_none(template):
    declared = set(defaults(template))
    for path in template_files(template):
        used = {match.strip() for match in ACTION_RE.findall(path.read_text())}
        assert {name.removeprefix(".") for name in used} <= declared, path
        if path.suffix == ".py":
            # wendy init renders .py files as Go templates, so "{{" would break init.
            assert "{{" not in path.read_text(), path


@pytest.mark.parametrize("template", [RECORD, POLICY], ids=["record", "policy"])
def test_template_manifest(template):
    data = manifest(template)
    assert data["name"] == template.name
    names = [variable["name"] for variable in data["variables"]]
    assert names[:2] == ["APP_ID", "PORT"] and "FOLLOWER_SERIAL_DEVICE" in names and "CALIBRATION_VOLUME" in names


def test_record_wendy_json():
    wendy = json.loads(render((RECORD / "wendy.json").read_text(), {**defaults(RECORD), "APP_ID": "rec"}))
    assert wendy["appId"] == "rec" and wendy["platform"] == "linux"
    assert {"type": "serial", "device": "ttyACM0"} in wendy["entitlements"]
    assert {"type": "serial", "device": "ttyACM1"} in wendy["entitlements"]
    assert {"type": "camera"} in wendy["entitlements"]
    assert {"type": "persist", "name": "rec-data", "path": "/data"} in wendy["entitlements"]
    assert {"type": "persist", "name": "so101-calibration-data", "path": "/calibration"} in wendy["entitlements"]
    assert wendy["readiness"]["tcpSocket"] == {"port": 8092}


def test_policy_wendy_json():
    wendy = json.loads(render((POLICY / "wendy.json").read_text(), {**defaults(POLICY), "APP_ID": "pol"}))
    types = [entitlement["type"] for entitlement in wendy["entitlements"]]
    assert types.count("serial") == 1 and "camera" in types and "gpu" in types
    assert {"type": "persist", "name": "pol-data", "path": "/data"} in wendy["entitlements"]
    assert {"type": "persist", "name": "so101-calibration-data", "path": "/calibration"} in wendy["entitlements"]
    assert wendy["readiness"]["tcpSocket"] == {"port": 8093}


def test_dockerfile_defaults_are_valid_settings():
    env = dockerfile_env(RECORD)
    s = record_settings.load(env)
    assert s.follower_port == "/dev/ttyACM0" and s.leader_port == "/dev/ttyACM1" and s.http_port == 8092
    assert env["HF_LEROBOT_HOME"] == "/data/lerobot"
    env = dockerfile_env(POLICY)
    s = policy_settings.load(env, is_dir=no_dirs)
    assert s.model_path == policy_settings.DEFAULT_MODEL and s.http_port == 8093
    assert env["HF_HOME"].startswith("/data/") and env["TORCH_HOME"].startswith("/data/")


@pytest.mark.parametrize(
    "template, sources",
    [
        (RECORD, ("record_settings.py", "record.py", "so101_common.py")),
        (POLICY, ("policy_settings.py", "run_policy.py", "so101_common.py")),
    ],
    ids=["record", "policy"],
)
def test_every_setting_is_documented_and_every_dockerfile_setting_is_read(template, sources):
    read = env_names_read_by(*(template / name for name in sources)) | {"HF_TOKEN"}
    readme = (template / "README.md").read_text()
    undocumented = sorted(name for name in read if f"`{name}`" not in readme)
    assert not undocumented, f"{template.name}/README.md does not document {undocumented}"
    library = {"HF_LEROBOT_HOME", "HF_HOME", "TORCH_HOME", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE"}
    unread = sorted(set(dockerfile_env(template)) - read - library)
    assert not unread, f"{template.name}/Dockerfile sets {unread}, which the app never reads"


@pytest.mark.parametrize("template", [RECORD, POLICY], ids=["record", "policy"])
def test_lerobot_and_python_versions_match_the_training_image(template):
    requirements = (template / "requirements.txt").read_text()
    assert re.search(r"^lerobot\[[a-z,]+\]==0\.6\.1$", requirements, re.MULTILINE)
    dockerfile = (template / "Dockerfile").read_text()
    assert "FROM python:3.12-slim-bookworm@sha256:" in dockerfile
    assert "torch==2.11.0 torchvision==0.26.0" in dockerfile


def test_policy_image_needs_no_dataset_extra_and_record_needs_no_gpu():
    assert "dataset" not in (POLICY / "requirements.txt").read_text().split("lerobot[", 1)[1].split("]")[0]
    assert "smolvla" in (POLICY / "requirements.txt").read_text()
    assert '"gpu"' not in (RECORD / "wendy.json").read_text()
