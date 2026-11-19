"""Check the elrobot-record and elrobot-policy templates without LeRobot or hardware.

The apps import LeRobot (and the vendored elrobot_arms module, which needs it)
only inside functions, so their modules load here with the standard library.
Hardware is replaced by fake servo buses that record every write.
"""

import ast
import importlib
import json
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "python"
RECORD = PYTHON / "elrobot-record"
POLICY = PYTHON / "elrobot-policy"
SHARED_MODULES = ("elrobot_common",)
SHARED_FILES = ("elrobot_common.py", "elrobot_arms.py", "UPSTREAM.md", "licenses/elrobot_teleoperation.LICENSE")
UPSTREAM_COMMIT = "2fe70041bb33775a574304c759602f218fb97222"


def load_modules(template, *names):
    """Import modules from one template directory, with that template's elrobot_common."""
    owned = SHARED_MODULES + names
    saved = {name: sys.modules.pop(name) for name in owned if name in sys.modules}
    sys.path.insert(0, str(template))
    try:
        modules = [importlib.import_module(name) for name in names]
        common = sys.modules["elrobot_common"]
    finally:
        sys.path.remove(str(template))
        for name in owned:
            sys.modules.pop(name, None)
        sys.modules.update(saved)
    return (common, *modules)


common, record_settings, record = load_modules(RECORD, "record_settings", "record")
pcommon, policy_settings, run_policy = load_modules(POLICY, "policy_settings", "run_policy")
# Each template loads its own copy of elrobot_common, so each has its own class.
SettingsError = (common.SettingsError, pcommon.SettingsError)
JOINTS = common.JOINTS
COMMIT = "4cbf28905b71f874a9e10123b01099c0e758bfcf"
OTHER_COMMIT = "0123456789abcdef0123456789abcdef01234567"


# ── fakes for LeRobot's servo bus and ElRobot arms ───────────────────────────


class FakeBus:
    """Records what a LeRobot FeetechMotorsBus would be asked to do.

    `present` holds normalized positions, `raw` the ticks the servos report.
    """

    def __init__(self, positions=None, raw=None, volts=12.4, mode=0, torque=0, offsets=None, limits=(0, 4095),
                 fail_goals=False):
        self.present = dict(positions or {joint: 0.0 for joint in JOINTS})
        self.raw = dict(raw or {joint: 2000 for joint in JOINTS})
        self.registers = {
            "Present_Voltage": {joint: round(volts * 10) for joint in JOINTS},
            "Operating_Mode": {joint: mode for joint in JOINTS},
            "Torque_Enable": {joint: torque for joint in JOINTS},
            "Homing_Offset": dict(offsets or {joint: 0 for joint in JOINTS}),
            "Min_Position_Limit": {joint: limits[0] for joint in JOINTS},
            "Max_Position_Limit": {joint: limits[1] for joint in JOINTS},
        }
        self.torque = bool(torque)
        self.is_connected = True
        self.fail_goals = fail_goals
        self.calls = []

    def sync_read(self, name, motors=None, normalize=True, num_retry=0):
        self.calls.append(("read", name))
        if name == "Present_Position":
            return dict(self.present) if normalize else dict(self.raw)
        return dict(self.registers[name])

    def sync_write(self, name, values, normalize=True, num_retry=0):
        assert name == "Goal_Position"
        if self.fail_goals:
            raise ConnectionError("servo 4 did not answer")
        self.calls.append(("goal" if normalize else "goal_raw", dict(values)))
        (self.present if normalize else self.raw).update(values)  # the servos reach every goal

    def read_calibration(self):
        self.calls.append(("read", "calibration"))
        return {
            joint: {
                "homing_offset": self.registers["Homing_Offset"][joint],
                "range_min": self.registers["Min_Position_Limit"][joint],
                "range_max": self.registers["Max_Position_Limit"][joint],
            }
            for joint in JOINTS
        }

    @property
    def goals(self):
        return [call[1] for call in self.calls if call[0] == "goal"]

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "read"]

    def disable_torque(self, motors=None, num_retry=0):
        self.calls.append(("torque", False, motors))
        self.torque = False

    def enable_torque(self, motors=None, num_retry=0):
        self.calls.append(("torque", True, motors))
        self.torque = True

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
    """Duck-types the ElRobot follower for the stop sequence."""

    def __init__(self, positions=None, fail_moves=False, cameras=None, goal_limits=None):
        self.bus = FakeBus(positions, fail_goals=fail_moves)
        self.cameras = cameras or {}
        self.goal_limits = goal_limits
        self.sent = []

    @property
    def is_connected(self):
        return self.bus.is_connected and all(camera.is_connected for camera in self.cameras.values())

    def send_action(self, action):
        if not self.is_connected:
            raise RuntimeError("not connected")
        self.sent.append(dict(action))
        self.bus.sync_write("Goal_Position", {key.removesuffix(".pos"): value for key, value in action.items()})
        return action

    def disconnect(self):
        if not self.is_connected:
            raise RuntimeError("not connected")
        self.bus.disconnect(True)
        for camera in self.cameras.values():
            camera.disconnect()


class Sleeps(list):
    def __call__(self, seconds):
        self.append(seconds)


def calibration(**changes):
    """A follower calibration that fits the URDF: every joint 1000..2800 (158 degrees), gripper 1000..2400."""
    data = {
        joint: {"id": index + 1, "drive_mode": 0, "homing_offset": 0, "range_min": 1000, "range_max": 2800}
        for index, joint in enumerate(JOINTS)
    }
    data["gripper"]["range_max"] = 2400
    for joint, fields in changes.items():
        data[joint].update(fields)
    return data


# ── shared files ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", SHARED_FILES)
def test_shared_files_are_identical(name):
    assert (RECORD / name).read_bytes() == (POLICY / name).read_bytes()


def test_joint_names_and_servo_ids():
    assert JOINTS == (
        "shoulder_yaw", "shoulder_pitch", "elbow_yaw", "elbow_pitch",
        "wrist_pitch", "wrist_roll", "wrist_yaw", "gripper",
    )
    assert common.SERVO_IDS == {joint: index for index, joint in enumerate(JOINTS, start=1)}
    assert set(common.URDF_LIMITS_DEG) == set(JOINTS)


def test_vendored_arms_use_the_same_motor_map():
    source = (RECORD / "elrobot_arms.py").read_text()
    motors = re.findall(r'"(\w+)": Motor\((\d), "sts3215"', source)
    expected = [(joint, str(common.SERVO_IDS[joint])) for joint in JOINTS]
    assert motors == expected * 2  # follower, then leader
    assert 'name = "elrobot_follower"' in source and 'name = "elrobot_leader"' in source
    assert 'register_subclass("elrobot_follower")' in source and 'register_subclass("elrobot_leader")' in source


def test_vendored_arms_keep_the_apache_header_and_name_their_source():
    source = (RECORD / "elrobot_arms.py").read_text()
    assert source.count("# Copyright 2025 The HuggingFace Inc. team. All rights reserved.") == 1
    assert 'Licensed under the Apache License, Version 2.0 (the "License");' in source
    assert UPSTREAM_COMMIT in source and UPSTREAM_COMMIT in (RECORD / "UPSTREAM.md").read_text()
    assert "Modified by Wendy Labs" in source
    license_text = (RECORD / "licenses" / "elrobot_teleoperation.LICENSE").read_text()
    assert "Apache License" in license_text and "Version 2.0, January 2004" in license_text
    ast.parse(source)


def test_vendored_arms_import_lerobot_only_from_the_installed_package():
    tree = ast.parse((RECORD / "elrobot_arms.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports only work inside the LeRobot source tree"


@pytest.mark.parametrize("raw, expected", [("", True), ("yes", True), ("ON", True), ("0", False), ("off", False)])
def test_env_bool(raw, expected):
    assert common.env_bool({"X": raw}, "X", True) is expected


@pytest.mark.parametrize("raw", ["maybe", "2"])
def test_env_bool_rejects_other_words(raw):
    with pytest.raises(SettingsError, match="yes/no"):
        common.env_bool({"X": raw}, "X", True)


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "ten"])
def test_env_float_rejects_non_finite(raw):
    with pytest.raises(SettingsError):
        common.env_float({"X": raw}, "X", 1.0)


def test_parse_cameras_needs_a_name_for_every_camera():
    assert common.parse_cameras("arm=/dev/video0, table=/dev/video2") == {
        "arm": "/dev/video0",
        "table": "/dev/video2",
    }
    assert common.parse_cameras("table=2") == {"table": "2"}


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "empty"),
        ("/dev/video0", "has no name"),
        ("Arm=/dev/video0", "lowercase"),
        ("arm=/dev/video0,arm=/dev/video2", "listed twice"),
        ("arm=/dev/video0,table=/dev/video0", "for two cameras"),
        ("arm=", "no device"),
        ("arm=video0", "device path"),
    ],
)
def test_parse_cameras_rejects(text, message):
    with pytest.raises(SettingsError, match=message):
        common.parse_cameras(text)


def test_max_relative_target_follows_the_urdf_velocity_unless_set():
    assert common.max_relative_target({}, 30) == 9.0  # 270 degrees/s at 30 commands/s
    assert common.max_relative_target({}, 15) == 18.0
    assert common.max_relative_target({"ELROBOT_MAX_RELATIVE_TARGET": "4.5"}, 30) == 4.5
    assert common.max_relative_target({"ELROBOT_MAX_RELATIVE_TARGET": "none"}, 30) is None
    with pytest.raises(SettingsError, match="below the minimum"):
        common.max_relative_target({"ELROBOT_MAX_RELATIVE_TARGET": "0"}, 30)
    with pytest.raises(SettingsError, match="not a number"):
        common.max_relative_target({"ELROBOT_MAX_RELATIVE_TARGET": "fast"}, 30)


def test_calibrate_role():
    assert common.calibrate_role({}, ("follower", "leader")) is None
    assert common.calibrate_role({"ELROBOT_CALIBRATE": "off"}, ("follower",)) is None
    assert common.calibrate_role({"ELROBOT_CALIBRATE": " Leader "}, ("follower", "leader")) == "leader"
    with pytest.raises(SettingsError, match="must be follower, or off"):
        common.calibrate_role({"ELROBOT_CALIBRATE": "leader"}, ("follower",))


def test_cross_site_posts_are_refused():
    assert common.same_origin({"Host": "robot:8094"})
    assert common.same_origin({"Host": "robot:8094", "Origin": "http://robot:8094"})
    assert not common.same_origin({"Host": "robot:8094", "Origin": "https://evil.example"})
    assert not common.same_origin({"Host": "robot:8094", "Sec-Fetch-Site": "cross-site"})


# ── calibration files ────────────────────────────────────────────────────────


def test_calibration_accepts_lerobot_shape():
    assert common.check_calibration(calibration(), "f", "follower") == []
    assert common.check_calibration(calibration(), "l", "leader") == []


@pytest.mark.parametrize(
    "data, message",
    [
        ({joint: {} for joint in ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")},
         "no entry for shoulder_yaw"),
        ({**calibration(), "shoulder_pan": {}}, "SO-101"),
        (calibration(gripper={"id": 6}), "servo id 6, expected 8"),
        (calibration(elbow_yaw={"range_min": 3000}), "not below range_max"),
        (calibration(wrist_yaw={"drive_mode": 2}), "drive_mode"),
        (calibration(shoulder_pitch={"homing_offset": True}), "whole number"),
        (calibration(elbow_pitch={"homing_offset": 2048}), "outside -2047..2047"),
        ([], "JSON object"),
    ],
)
def test_calibration_rejects(data, message):
    with pytest.raises(SettingsError, match=message):
        common.check_calibration(data, "f", "follower")


def test_follower_ranges_wider_than_the_urdf_are_refused():
    # shoulder_yaw travels 177.8 degrees; 2400 ticks is 211 degrees: a sweep across the encoder boundary.
    with pytest.raises(SettingsError, match="shoulder_yaw spans 211 degrees.*travels 177.8"):
        common.check_calibration(calibration(shoulder_yaw={"range_min": 800, "range_max": 3200}), "f", "follower")
    # The leader has no URDF, so the same file is accepted for it.
    assert common.check_calibration(calibration(shoulder_yaw={"range_min": 800, "range_max": 3200}), "l", "leader") == []


def test_full_turn_wrist_roll_from_upstream_lerobot_calibrate_is_accepted_with_a_warning():
    warnings = common.check_calibration(calibration(wrist_roll={"range_min": 0, "range_max": 4095}), "f", "follower")
    assert len(warnings) == 1 and "wrist_roll" in warnings[0] and "full turn" in warnings[0]


def test_missing_calibration_says_how_to_calibrate(tmp_path):
    (tmp_path / "leader.json").write_text(json.dumps(calibration()))
    with pytest.raises(SettingsError, match="holds: leader.json.*ELROBOT_CALIBRATE=follower"):
        common.load_calibration(tmp_path / "follower.json", "follower", "ELROBOT_FOLLOWER_CALIBRATION")
    loaded = common.load_calibration(tmp_path / "leader.json", "leader", "ELROBOT_LEADER_CALIBRATION")
    assert loaded.data["gripper"]["id"] == 8


def test_calibration_differences():
    servos = {joint: {"homing_offset": 0, "range_min": 0, "range_max": 4095} for joint in JOINTS}
    offsets, limits = common.calibration_differences(servos, calibration(elbow_yaw={"homing_offset": 12}))
    assert offsets == ["elbow_yaw"] and limits == list(JOINTS)


def test_goal_limits_in_degrees_are_the_calibrated_range_around_its_middle():
    limits = common.goal_limits(calibration(), use_degrees=True)
    # 1000..2800 ticks: 900 ticks either side of the middle = 79.1 degrees (LeRobot divides by 4095/360).
    assert limits["shoulder_yaw"] == pytest.approx((-900 * 360 / 4095, 900 * 360 / 4095))
    assert limits["gripper"] == (0.0, 100.0)
    assert common.goal_limits(calibration(), use_degrees=False)["elbow_yaw"] == (-100.0, 100.0)


def test_clamp_goals():
    limits = {"shoulder_yaw": (-80.0, 80.0), "gripper": (0.0, 100.0)}
    goals = {"shoulder_yaw.pos": 120.0, "gripper.pos": -5.0, "elbow_yaw.pos": 500.0, "note": "x"}
    assert common.clamp_goals(goals, limits) == {
        "shoulder_yaw.pos": 80.0, "gripper.pos": 0.0, "elbow_yaw.pos": 500.0, "note": "x",
    }
    assert common.clamp_goals(goals, None) == goals


def test_write_calibration_file_is_atomic_and_keeps_the_old_file(tmp_path):
    path = tmp_path / "follower.json"
    assert common.write_calibration_file(path, {"a": 1}) is None
    backup = common.write_calibration_file(path, {"a": 2})
    assert json.loads(path.read_text()) == {"a": 2} and json.loads(Path(backup).read_text()) == {"a": 1}
    assert sorted(p.name for p in tmp_path.glob("*.json")) == ["follower.json"]


# ── calibration mode: homing math and range capture ─────────────────────────


@pytest.mark.parametrize("present, servo_offset", [(2047, 0), (100, 0), (4000, 0), (2869, -1099), (1786, 1868), (0, -2047)])
def test_a_new_home_reads_2047_once_written(present, servo_offset):
    offset = common.offset_for_home(present, servo_offset)
    assert -2047 <= offset <= 2047
    assert common.in_frame(present, servo_offset, offset) in (2047, 2046)  # 2046 only for the -2048 edge


def test_offset_for_home_matches_lerobot_half_turn_homing_on_a_factory_servo():
    # LeRobot: reset the offset to 0, then offset = present - 2047.
    assert common.offset_for_home(3000, 0) == 3000 - 2047
    assert common.offset_for_home(1000, 0) == 1000 - 2047


def sweep(capture, joint, values):
    for value in values:
        capture.update({joint: value})


def test_range_capture_records_a_sweep_including_the_first_readings():
    capture = common.RangeCapture()
    sweep(capture, "elbow_yaw", range(1000, 3001, 20))
    assert (capture.low["elbow_yaw"], capture.high["elbow_yaw"]) == (1000, 3000)
    assert not capture.wrapped


def test_range_capture_ignores_a_one_read_glitch():
    capture = common.RangeCapture()
    sweep(capture, "elbow_yaw", [2000] * 5 + [2010, 4095, 2020, 2030, 0, 2040])
    assert (capture.low["elbow_yaw"], capture.high["elbow_yaw"]) == (2000, 2040)
    assert not capture.wrapped


def test_range_capture_marks_a_crossing_of_the_encoder_boundary():
    capture = common.RangeCapture()
    sweep(capture, "wrist_pitch", list(range(3900, 4096, 15)) + list(range(0, 200, 15)))
    assert "wrist_pitch" in capture.wrapped
    status = common.joint_status("wrist_pitch", capture.low["wrist_pitch"], capture.high["wrist_pitch"], True, "leader")
    assert status == "wrapped"


def test_range_capture_follows_a_real_jump_after_a_stall():
    capture = common.RangeCapture()
    sweep(capture, "gripper", [1500] * 5 + [2200] * 5)
    assert (capture.low["gripper"], capture.high["gripper"]) == (1500, 2200)
    assert not capture.wrapped


def test_joint_status():
    assert common.joint_status("elbow_yaw", None, None, False, "follower") == "not swept"
    assert common.joint_status("elbow_yaw", 1000, 1300, False, "follower") == "narrow"
    assert common.joint_status("elbow_yaw", 1000, 3000, False, "follower") == "ok"
    assert common.joint_status("shoulder_yaw", 500, 3500, False, "follower") == "too wide"
    assert common.joint_status("shoulder_yaw", 500, 3500, False, "leader") == "ok"
    assert common.joint_status("gripper", 1000, 1300, False, "follower") == "ok"


class CalibrationBus:
    """A bus for calibration mode: readable registers; any write fails the test."""

    def __init__(self, offsets=None, present=None, torque=0):
        self.registers = {name: {joint: 0 for joint in JOINTS} for name in common.PROBE_REGISTERS}
        self.registers["Homing_Offset"].update(offsets or {})
        self.registers["Present_Voltage"] = {joint: 124 for joint in JOINTS}
        self.registers["Torque_Enable"] = {joint: torque for joint in JOINTS}
        self.registers["Present_Position"] = dict(present or {joint: 2000 for joint in JOINTS})
        self.closed_with = None

    def sync_read(self, name, motors=None, normalize=True, num_retry=0):
        assert normalize is False
        return dict(self.registers[name])

    def move(self, **positions):
        self.registers["Present_Position"].update(positions)

    def disconnect(self, disable_torque=True):
        self.closed_with = disable_torque

    def __getattr__(self, name):
        if name in ("write", "sync_write", "write_calibration", "disable_torque", "enable_torque", "reset_calibration",
                    "set_half_turn_homings"):
            raise AssertionError(f"calibration mode called bus.{name}")
        raise AttributeError(name)


def calibrated_session(tmp_path, role="follower", bus=None, spans=None):
    bus = bus or CalibrationBus(offsets={"wrist_roll": 1868, "wrist_yaw": -1099})
    session = common.CalibrationSession(role, "/dev/ttyACM1", tmp_path / f"{role}.json", open_bus=lambda port: bus)
    assert session.connect_once()
    session.start()
    # 1800 ticks (158 degrees) per joint fits every follower joint; the gripper travels 126 degrees.
    spans = spans or {**{joint: (1100, 2900) for joint in JOINTS}, "gripper": (1100, 2400)}
    for step in range(0, 101, 2):
        bus.move(**{joint: low + (high - low) * step // 100 for joint, (low, high) in spans.items()})
        session.capture.update(session.frame(bus.registers["Present_Position"]))
    session.stop()
    return session, bus


def test_calibration_mode_keeps_the_servos_offsets_and_never_writes(tmp_path):
    session, bus = calibrated_session(tmp_path)
    assert "saved" in session.save()
    data = json.loads((tmp_path / "follower.json").read_text())
    assert data["wrist_roll"] == {"id": 6, "drive_mode": 0, "homing_offset": 1868, "range_min": 1100, "range_max": 2900}
    assert data["shoulder_yaw"]["homing_offset"] == 0
    assert common.check_calibration(data, "f", "follower") == []
    assert session.saved["writes_needed"] is False
    session.close()
    assert bus.closed_with is False  # closing the port must not turn torque off (a write)


def test_calibration_mode_new_home_goes_into_the_file_only(tmp_path):
    bus = CalibrationBus(present={joint: 3000 for joint in JOINTS})
    session = common.CalibrationSession("leader", "/dev/ttyACM0", tmp_path / "leader.json", open_bus=lambda port: bus)
    session.connect_once()
    assert "nothing written" in session.set_home()
    assert session.offsets() == {joint: 3000 - 2047 for joint in JOINTS}
    session.start()
    for step in range(0, 101, 2):
        bus.move(**{joint: 2400 + step * 12 for joint in JOINTS})
        session.capture.update(session.frame(bus.registers["Present_Position"]))
    session.stop()
    session.save()
    data = json.loads((tmp_path / "leader.json").read_text())
    # The home pose (3000 ticks now) will read 2047 once the new offset is in the servo.
    assert data["elbow_yaw"]["homing_offset"] == 953
    assert (data["elbow_yaw"]["range_min"], data["elbow_yaw"]["range_max"]) == (2400 - 953, 3600 - 953)
    assert session.saved["writes_needed"] is True


def test_calibration_mode_refuses_unswept_wrapped_or_too_wide_ranges(tmp_path):
    session, _bus = calibrated_session(tmp_path, spans={joint: (1100, 2900) for joint in JOINTS[:-1]})
    with pytest.raises(SettingsError, match="gripper not swept"):
        session.save()
    session, _bus = calibrated_session(tmp_path, spans={**{j: (1100, 2400) for j in JOINTS}, "shoulder_yaw": (400, 3600)})
    with pytest.raises(SettingsError, match="shoulder_yaw too wide.*Set home here"):
        session.save()
    assert not (tmp_path / "follower.json").exists()


def test_calibration_mode_refuses_to_save_while_recording_or_after_the_offsets_changed(tmp_path):
    session, bus = calibrated_session(tmp_path)
    session.start()
    with pytest.raises(SettingsError, match="stop recording"):
        session.save()
    session, bus = calibrated_session(tmp_path)
    bus.registers["Homing_Offset"]["elbow_yaw"] = 5
    with pytest.raises(SettingsError, match="changed since"):
        session.save()


def test_calibration_mode_waits_for_the_arm(tmp_path):
    def unpowered(_port):
        raise RuntimeError("FeetechMotorsBus motor check failed on port '/dev/ttyACM0':\n\nMissing motor IDs:")

    session = common.CalibrationSession("leader", "/dev/ttyACM0", tmp_path / "leader.json", open_bus=unpowered)
    assert not session.connect_once()
    assert "servo supply" in session.status()["detail"]
    with pytest.raises(SettingsError, match="not connected yet"):
        session.start()


def test_calibration_status_lists_every_joint(tmp_path):
    session, _bus = calibrated_session(tmp_path)
    status = session.status()
    assert [row["joint"] for row in status["joints"]] == list(JOINTS)
    assert status["joints"][0]["urdf_deg"] == 177.8 and status["joints"][0]["status"] == "ok"
    assert status["mode"] == "calibrate" and "nothing to write" in status["home"]


# ── checks before torque goes on, and the arm subclass ───────────────────────


def test_supply_voltage_tells_the_arms_apart():
    assert common.check_supply(FakeBus(volts=12.4), "follower", "/dev/ttyACM1") == "12.4 to 12.4 V"
    assert common.check_supply(FakeBus(volts=7.4), "leader", "/dev/ttyACM0") == "7.4 to 7.4 V"
    with pytest.raises(SettingsError, match="looks like the leader"):
        common.check_supply(FakeBus(volts=5.0), "follower", "/dev/ttyACM0")
    with pytest.raises(SettingsError, match="looks like the follower"):
        common.check_supply(FakeBus(volts=12.3), "leader", "/dev/ttyACM1")


def test_position_mode_and_range_checks():
    common.check_position_mode(FakeBus(mode=0))
    with pytest.raises(SettingsError, match="not position mode.*ELROBOT_CONFIGURE_SERVOS"):
        common.check_position_mode(FakeBus(mode=1))
    common.check_within_range(FakeBus(raw={joint: 1000 - 50 for joint in JOINTS}), calibration())
    with pytest.raises(SettingsError, match="elbow_pitch reads 3000, calibrated 1000..2800"):
        common.check_within_range(FakeBus(raw={**{j: 2000 for j in JOINTS}, "elbow_pitch": 3000}), calibration())


def test_hold_writes_the_raw_present_position():
    bus = FakeBus(raw={joint: 1500 + index for index, joint in enumerate(JOINTS)})
    common.hold_present_position(bus)
    assert bus.writes == [("goal_raw", {joint: 1500 + index for index, joint in enumerate(JOINTS)})]


def test_leader_torque_is_turned_off_only_where_it_is_on():
    bus = FakeBus(torque=0)
    assert common.release_leader_torque(bus) == [] and bus.writes == []
    bus = FakeBus(torque=1)
    assert common.release_leader_torque(bus) == list(JOINTS)
    assert bus.writes == [("torque", False, list(JOINTS))]


class FakeLeRobotArm:
    """Stands in for the vendored ElRobot classes: calibrate() would prompt, configure() writes EEPROM."""

    def __init__(self, calibration_data, bus=None, use_degrees=True):
        self.bus = bus or FakeBus(raw={joint: 1900 for joint in JOINTS})
        self.calibration = calibration_data
        self.calibration_fpath = Path("/calibration/follower.json")
        self.config = type("Config", (), {"port": "/dev/ttyACM1", "use_degrees": use_degrees})()
        self.id = "follower"

    def calibrate(self):
        raise AssertionError("the integration's calibrate() waits for terminal input")

    def configure(self):
        self.bus.calls.append(("integration configure",))

    def send_action(self, action):
        self.bus.calls.append(("send", dict(action)))
        return action


def arm(role="follower", data=None, bus=None, **options):
    cls = common.wendy_arm(FakeLeRobotArm, common.ArmOptions(role=role, **options))
    return cls(calibration() if data is None else data, bus=bus)


def test_calibrate_without_a_file_refuses():
    with pytest.raises(SettingsError, match="no calibration file"):
        arm(data={}).calibrate()


def test_calibrate_with_matching_offsets_writes_nothing():
    follower = arm()
    follower.calibrate()  # only the position limits differ (0..4095 on the servos)
    assert follower.bus.writes == []
    assert type(follower).__name__ == "FakeLeRobotArmWendy"


def test_calibrate_with_different_offsets_refuses_unless_writes_are_allowed():
    follower = arm(data=calibration(elbow_yaw={"homing_offset": -793}))
    with pytest.raises(SettingsError, match="homing offsets differ .* for elbow_yaw.*ELROBOT_WRITE_CALIBRATION=true"):
        follower.calibrate()
    assert follower.bus.writes == []
    follower = arm(data=calibration(elbow_yaw={"homing_offset": -793}), write_calibration=True)
    follower.calibrate()
    assert follower.bus.writes == [("torque", False, None), ("calibration", follower.calibration)]


def test_calibrate_checks_the_supply_before_writing():
    follower = arm(bus=FakeBus(volts=5.0), data=calibration(elbow_yaw={"homing_offset": -793}), write_calibration=True)
    with pytest.raises(SettingsError, match="looks like the leader"):
        follower.calibrate()
    assert follower.bus.writes == []


def test_follower_configure_holds_then_turns_torque_on():
    follower = arm()
    follower.configure()
    assert follower.bus.writes == [("goal_raw", {joint: 1900 for joint in JOINTS}), ("torque", True, None)]
    assert follower.goal_limits["shoulder_yaw"] == pytest.approx((-79.1, 79.1), abs=0.1)


def test_follower_configure_refuses_before_any_write():
    for bus, message in (
        (FakeBus(volts=7.0, raw={j: 1900 for j in JOINTS}), "looks like the leader"),
        (FakeBus(mode=3, raw={j: 1900 for j in JOINTS}), "not position mode"),
        (FakeBus(raw={**{j: 1900 for j in JOINTS}, "gripper": 3500}), "outside their calibrated range"),
    ):
        follower = arm(bus=bus)
        with pytest.raises(SettingsError, match=message):
            follower.configure()
        assert bus.writes == []


def test_configure_servos_runs_the_integration_configure_after_the_hold():
    follower = arm(configure_servos=True, bus=FakeBus(mode=3, raw={j: 1900 for j in JOINTS}))
    follower.configure()  # position mode is not required: the integration writes it
    assert [call[0] for call in follower.bus.writes] == ["goal_raw", "integration configure"]
    leader = arm(role="leader", configure_servos=True, bus=FakeBus(volts=5.0))
    leader.configure()
    assert leader.bus.writes == [("integration configure",)]


def test_leader_configure_only_releases_torque():
    leader = arm(role="leader", bus=FakeBus(volts=5.0, mode=2, raw={j: 4000 for j in JOINTS}))
    leader.configure()  # a leader is only read: its mode and range do not matter
    assert leader.bus.writes == []
    assert "send_action" not in type(leader).__dict__  # only the follower's goals are clamped
    leader = arm(role="leader", bus=FakeBus(volts=5.0, torque=1))
    leader.configure()
    assert leader.bus.writes == [("torque", False, list(JOINTS))]


def test_voltage_check_can_be_turned_off():
    follower = arm(bus=FakeBus(volts=5.0, raw={j: 1900 for j in JOINTS}), check_voltage=False)
    follower.configure()
    assert ("read", "Present_Voltage") not in follower.bus.calls


def test_follower_send_action_is_clamped_to_the_calibrated_range():
    follower = arm()
    follower.configure()
    follower.send_action({"shoulder_yaw.pos": 170.0, "gripper.pos": 140.0, "elbow_yaw.pos": 10.0})
    sent = follower.bus.calls[-1]
    assert sent[0] == "send"
    assert sent[1]["shoulder_yaw.pos"] == pytest.approx(79.1, abs=0.1)
    assert sent[1]["gripper.pos"] == 100.0 and sent[1]["elbow_yaw.pos"] == 10.0


# ── read-only probe report ───────────────────────────────────────────────────


def registers(offsets=None, present=None, volts=124, torque=0, mode=0):
    values = {name: {joint: 0 for joint in JOINTS} for name in common.PROBE_REGISTERS}
    values["Homing_Offset"].update(offsets or {})
    values["Max_Position_Limit"] = {joint: 4095 for joint in JOINTS}
    values["Present_Position"] = dict(present or {joint: 1900 for joint in JOINTS})
    values["Present_Voltage"] = {joint: volts for joint in JOINTS}
    values["Torque_Enable"] = {joint: torque for joint in JOINTS}
    values["Operating_Mode"] = {joint: mode for joint in JOINTS}
    return values


def report(role="follower", regs=None, data=None, **options):
    cal = common.CalibrationFile(Path("/calibration/follower.json"), calibration() if data is None else data)
    return common.describe_arm(role, "/dev/ttyACM1", regs or registers(), cal, common.ArmOptions(role=role, **options))


def test_probe_report_lists_every_servo_and_what_connecting_would_write():
    lines = report()
    assert lines[0].startswith("follower /dev/ttyACM1: 8/8 servos answered, 12.4 to 12.4 V, torque off")
    assert sum(1 for line in lines if "(id " in line) == 8
    assert "follower: homing offsets match the file" in lines
    assert any("position limits differ" in line and "limited in software" in line for line in lines)
    assert any("writes Goal_Position" in line for line in lines)
    assert not any("WOULD WRITE" in line for line in lines)


def test_probe_report_flags_problems():
    lines = report(regs=registers(volts=50, offsets={"elbow_yaw": 7}))
    assert any("PROBLEM" in line and "swapped ports" in line for line in lines)
    assert any("homing offsets differ" in line and "would stop" in line for line in lines)
    lines = report(regs=registers(offsets={"elbow_yaw": 7}), write_calibration=True, configure_servos=True)
    assert sum("WOULD WRITE" in line for line in lines) == 2
    lines = report(regs=registers(present={**{j: 1900 for j in JOINTS}, "wrist_roll": 100}))
    assert any("outside the calibrated range now: wrist_roll" in line for line in lines)
    assert common.planned_writes("leader", registers(), None, common.ArmOptions(role="leader")) == [
        "leader: no usable calibration file, so connecting would stop before any write"
    ]


# ── the stop sequence, against a fake servo bus ──────────────────────────────


def test_move_seconds_scales_with_the_farthest_joint():
    assert common.move_seconds({"a.pos": 0.0}, {"a.pos": 10.0}) == 2.0
    assert common.move_seconds({"a.pos": 0.0, "b.pos": 0.0}, {"a.pos": 10.0, "b.pos": 180.0}) == 4.0
    assert common.move_seconds({"a.pos": 0.0}, {"a.pos": 900.0}) == 8.0


def test_move_smoothly_interpolates_from_the_present_pose():
    arm_ = FakeArm({joint: 0.0 for joint in JOINTS})
    sleeps = Sleeps()
    target = {"shoulder_yaw.pos": 30.0, "gripper.pos": 50.0, "not_a_joint.pos": 1.0}
    sent = common.move_smoothly(arm_, target, seconds=1.0, fps=10, sleep=sleeps)
    assert sent == 10 and len(arm_.bus.goals) == 10 and sleeps == [0.1] * 10
    assert arm_.bus.goals[0] == pytest.approx({"shoulder_yaw": 3.0, "gripper": 5.0})
    assert arm_.bus.goals[-1] == {"shoulder_yaw": 30.0, "gripper": 50.0}
    # It reads and writes the servo bus itself, so a failed camera cannot block it.
    assert arm_.bus.calls[0] == ("read", "Present_Position") and arm_.sent == []


def test_move_smoothly_clamps_the_target_to_the_calibrated_range():
    arm_ = FakeArm(goal_limits={"shoulder_yaw": (-80.0, 80.0)})
    common.move_smoothly(arm_, {"shoulder_yaw.pos": 150.0}, 0.2, fps=10, sleep=Sleeps())
    assert arm_.bus.goals[-1] == {"shoulder_yaw": 80.0}


def test_move_smoothly_works_after_a_camera_failed():
    arm_ = FakeArm(cameras={"arm": FakeCamera(connected=False)})
    assert not arm_.is_connected
    assert common.move_smoothly(arm_, {"gripper.pos": 9.0}, 0.2, fps=10, sleep=Sleeps()) == 2
    assert arm_.bus.present["gripper"] == 9.0


def test_move_smoothly_can_be_cut_short_or_skipped():
    arm_ = FakeArm()
    calls = iter([False, False, True])
    assert common.move_smoothly(arm_, {"gripper.pos": 9.0}, 1.0, fps=10, sleep=Sleeps(),
                                should_stop=lambda: next(calls)) == 2
    assert common.move_smoothly(arm_, {"gripper.pos": 9.0}, 0, sleep=Sleeps()) == 0
    assert common.move_smoothly(arm_, {}, 1.0, sleep=Sleeps()) == 0


def test_safe_stop_returns_to_the_start_pose_then_turns_torque_off():
    rest = {f"{joint}.pos": 0.0 for joint in JOINTS}
    arm_ = FakeArm({joint: 40.0 for joint in JOINTS}, cameras={"arm": FakeCamera(), "table": FakeCamera()})
    report_ = common.safe_stop(arm_, rest, seconds=2.0, fps=5, sleep=Sleeps())
    assert len(arm_.bus.goals) == 10 and arm_.bus.goals[-1] == {joint: 0.0 for joint in JOINTS}
    assert arm_.bus.calls[-1] == ("disconnect", True)
    assert not arm_.bus.torque and not arm_.bus.is_connected
    assert not any(camera.is_connected for camera in arm_.cameras.values())
    assert report_ == "arm returned to its start pose; torque off"


def test_safe_stop_turns_torque_off_even_when_the_move_fails():
    arm_ = FakeArm(fail_moves=True)
    report_ = common.safe_stop(arm_, {"gripper.pos": 0.0}, seconds=1.0, sleep=Sleeps())
    assert "could not return to its start pose (servo 4 did not answer)" in report_
    assert report_.endswith("torque off")
    assert not arm_.bus.torque and not arm_.bus.is_connected


def test_safe_stop_without_a_start_pose_only_releases():
    arm_ = FakeArm()
    assert common.safe_stop(arm_, None, seconds=3.0, sleep=Sleeps()) == "arm not moved; torque off"
    assert arm_.bus.goals == [] and not arm_.bus.torque


def test_release_falls_back_to_the_bus_when_disconnect_fails():
    arm_ = FakeArm()

    def broken_disconnect():
        raise OSError("port busy")

    arm_.disconnect = broken_disconnect
    assert common.release(arm_) is True
    assert arm_.bus.calls[-1] == ("disconnect", True) and not arm_.bus.torque


def test_release_after_a_half_finished_connect():
    arm_ = FakeArm(cameras={"arm": FakeCamera(connected=False)})
    assert not arm_.is_connected
    assert common.release(arm_) is True
    assert arm_.bus.calls == [("disconnect", True)]


def test_release_of_a_closed_arm_does_nothing():
    arm_ = FakeArm()
    arm_.bus.is_connected = False
    assert common.release(arm_) is True
    assert arm_.bus.calls == []


# ── recorder settings ────────────────────────────────────────────────────────


def test_recorder_defaults_are_the_teams_setup():
    s = record_settings.load({})
    assert (s.follower_port, s.leader_port) == ("/dev/ttyACM1", "/dev/ttyACM0")
    assert s.cameras == {"arm": "/dev/video0", "table": "/dev/video2"} and s.camera_fourcc == "MJPG"
    assert s.repo_id == "local/elrobot_demos" and s.dataset_root == Path("/data/lerobot/local/elrobot_demos")
    assert s.push is False and "local dataset only" in s.push_reason
    assert (s.fps, s.num_episodes, s.episode_seconds, s.reset_seconds, s.return_seconds) == (30, 50, 30.0, 10.0, 3.0)
    assert s.max_relative_target == 9.0 and s.http_port == 8094 and s.task == common.DEFAULT_TASK
    assert s.calibrate is None and not s.write_calibration and not s.configure_servos and s.check_voltage
    assert s.private and s.use_degrees and s.streaming_encoding and not s.dry_run
    assert s.video_codec == "libsvtav1"
    assert s.calibration_target() == ("/dev/ttyACM1", Path("/calibration/follower.json"))


def test_recorder_calibration_target():
    s = record_settings.load({"ELROBOT_CALIBRATE": "leader"})
    assert s.calibrate == "leader" and s.calibration_target() == ("/dev/ttyACM0", Path("/calibration/leader.json"))


def test_recorder_arm_options_follow_the_settings():
    s = record_settings.load({"ELROBOT_WRITE_CALIBRATION": "true", "ELROBOT_CHECK_VOLTAGE": "false"})
    assert record.arm_options(s, "leader") == common.ArmOptions(
        role="leader", write_calibration=True, configure_servos=False, check_voltage=False
    )


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


@pytest.mark.parametrize(
    "env, message",
    [
        ({"ELROBOT_PUSH_TO_HUB": "maybe"}, "auto, true or false"),
        ({"ELROBOT_DATASET_REPO_ID": "demos"}, "not a Hugging Face repo id"),
        ({"ELROBOT_DATASET_REPO_ID": "team/eval_demos"}, "reserved"),
        ({"ELROBOT_LEADER_PORT": "/dev/ttyACM1"}, "both /dev/ttyACM1"),
        ({"ELROBOT_FOLLOWER_PORT": "ttyACM1"}, "device path"),
        ({"ELROBOT_LEADER_CALIBRATION": "/calibration/follower.json"}, "its own calibration"),
        ({"ELROBOT_VIDEO_CODEC": "vp9"}, "must be one of"),
        ({"ELROBOT_CAMERA_FOURCC": "MJPEG"}, "4 characters"),
        ({"ELROBOT_CAMERAS": "/dev/video0"}, "has no name"),
        ({"ELROBOT_FPS": "0"}, "below the minimum"),
        ({"ELROBOT_EPISODE_SECONDS": "0.5"}, "below the minimum"),
        ({"ELROBOT_RETURN_SECONDS": "6"}, "above the maximum 5"),
        ({"ELROBOT_NUM_EPISODES": "many"}, "whole number"),
        ({"ELROBOT_TASK": "   "}, "ELROBOT_TASK is empty"),
        ({"ELROBOT_CALIBRATE": "both"}, "follower or leader, or off"),
        ({"ELROBOT_WRITE_CALIBRATION": "please"}, "yes/no"),
    ],
)
def test_recorder_rejects(env, message):
    with pytest.raises(SettingsError, match=message):
        record_settings.load(env)


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
    recorder = record.Recorder(record_settings.load({"ELROBOT_DATASET_REPO_ID": "team/d", "HF_TOKEN": "x"}))
    recorder.state = "recording"
    with pytest.raises(SettingsError, match="once the session has finished"):
        recorder.push()


def test_recorder_status_lists_the_cameras():
    status = record.Recorder(record_settings.load({})).status()
    assert [camera["name"] for camera in status["cameras"]] == ["arm", "table"]
    assert status["cameras"][0]["taken"] is None


# ── policy settings ──────────────────────────────────────────────────────────


def no_dirs(_path):
    return False


MODEL = {"ELROBOT_MODEL_PATH": "team/elrobot-act"}


def test_policy_needs_a_model_except_to_calibrate():
    with pytest.raises(SettingsError, match="ELROBOT_MODEL_PATH is not set. There is no public ElRobot model"):
        policy_settings.load({}, is_dir=no_dirs)
    s = policy_settings.load({"ELROBOT_CALIBRATE": "follower"}, is_dir=no_dirs)
    assert s.calibrate == "follower" and s.model_path is None


def test_policy_defaults():
    s = policy_settings.load(MODEL, is_dir=no_dirs)
    assert s.model_path == "team/elrobot-act" and s.model_revision is None and not s.model_is_local
    assert s.follower_port == "/dev/ttyACM1" and s.follower_calibration == Path("/calibration/follower.json")
    assert s.cameras == {"arm": "/dev/video0", "table": "/dev/video2"}
    assert (s.camera_width, s.camera_height, s.camera_fourcc) == (None, None, "MJPG")
    assert (s.fps, s.device, s.max_relative_target, s.return_seconds, s.run_seconds) == (30, "auto", 9.0, 3.0, 0.0)
    assert not s.autostart and not s.dry_run and s.use_degrees
    assert not s.write_calibration and not s.configure_servos and s.check_voltage and s.calibrate is None
    assert s.http_port == 8095 and s.task == common.DEFAULT_TASK


def test_model_source():
    def dirs(path):
        return path in ("/models/act", "checkpoints/act")

    assert policy_settings.model_source("/models/act", None, is_dir=dirs) is True
    assert policy_settings.model_source("checkpoints/act", None, is_dir=dirs) is True
    assert policy_settings.model_source("team/elrobot-act", "v2", is_dir=dirs) is False
    with pytest.raises(SettingsError, match="applies to Hub models only"):
        policy_settings.model_source("/models/act", "main", is_dir=dirs)
    with pytest.raises(SettingsError, match="not a directory in the container"):
        policy_settings.model_source("./missing", None, is_dir=dirs)
    for bad in ("act", "team/elrobot act", "team/../x", "team/a--b"):
        with pytest.raises(SettingsError, match="neither a Hugging Face model id"):
            policy_settings.model_source(bad, None, is_dir=dirs)


@pytest.mark.parametrize("revision", ["main", "v1.0", COMMIT, "refs/pr/3", "feature_x"])
def test_valid_revisions(revision):
    assert policy_settings.check_revision(revision) == revision


@pytest.mark.parametrize("revision", ["-main", "a b", "main..dev", "dev/", "/main", "x.lock"])
def test_invalid_revisions(revision):
    with pytest.raises(SettingsError, match="not a branch, tag or commit"):
        policy_settings.check_revision(revision)


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
    assert policy_settings.resolve_revision("team/act", COMMIT, lookup_returning(COMMIT)) == (COMMIT, f"commit {COMMIT}")


def test_resolve_offline():
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


@pytest.mark.parametrize("policy_type", ["act", "smolvla"])
def test_supported_policy_types(policy_type):
    assert policy_settings.check_policy_type(policy_type, "m") == policy_type


def test_other_policy_types_are_refused_with_a_hint():
    with pytest.raises(SettingsError, match="'diffusion' policy; this app runs act and smolvla"):
        policy_settings.check_policy_type("diffusion", "m")


ACT_INPUTS = {
    "observation.state": ("STATE", (8,)),
    "observation.images.arm": ("VISUAL", (3, 480, 640)),
    "observation.images.table": ("VISUAL", (3, 480, 640)),
}
ACTION = {"action": ("ACTION", (8,))}


def test_model_features():
    assert policy_settings.check_model_features(ACT_INPUTS, ACTION, "m") == {"arm": (480, 640), "table": (480, 640)}
    smolvla = {**ACT_INPUTS, "observation.images.empty_camera_0": ("VISUAL", (3, 480, 640))}
    assert set(policy_settings.check_model_features(smolvla, ACTION, "m")) == {"arm", "table"}


@pytest.mark.parametrize(
    "inputs, outputs, message",
    [
        ({**ACT_INPUTS, "observation.state": ("STATE", (6,))}, ACTION, "8-joint observation.state.*SO-101"),
        (ACT_INPUTS, {"action": ("ACTION", (6,))}, "8-joint action"),
        ({"observation.state": ("STATE", (8,))}, ACTION, "no camera image"),
        ({**ACT_INPUTS, "observation.environment_state": ("ENV", (10,))}, ACTION, "does not provide"),
        ({**ACT_INPUTS, "observation.images.depth": ("VISUAL", (1, 480, 640))}, ACTION, "color images"),
    ],
)
def test_model_features_rejected(inputs, outputs, message):
    with pytest.raises(SettingsError, match=message):
        policy_settings.check_model_features(inputs, outputs, "m")


def test_match_cameras_needs_the_models_names():
    two = {"arm": (480, 640), "table": (480, 640)}
    named = {"arm": "/dev/video2", "table": "/dev/video0"}
    assert policy_settings.match_cameras(named, two) == named
    with pytest.raises(SettingsError, match="names arm, but the model expects arm, table"):
        policy_settings.match_cameras({"arm": "/dev/video0"}, two)
    with pytest.raises(SettingsError, match="but the model expects wrist"):
        policy_settings.match_cameras(named, {"wrist": (480, 640)})


def test_camera_size_follows_the_model_unless_set():
    s = policy_settings.load(MODEL, is_dir=no_dirs)
    assert policy_settings.camera_size(s, (480, 640)) == (640, 480)
    s = policy_settings.load({**MODEL, "ELROBOT_CAMERA_WIDTH": "320", "ELROBOT_CAMERA_HEIGHT": "240"}, is_dir=no_dirs)
    assert policy_settings.camera_size(s, (480, 640)) == (320, 240)


def test_choose_device():
    assert policy_settings.choose_device("auto", True) == "cuda"
    assert policy_settings.choose_device("auto", False) == "cpu"
    with pytest.raises(SettingsError, match="sees no CUDA GPU"):
        policy_settings.choose_device("cuda", False)


@pytest.mark.parametrize(
    "env, message",
    [
        ({"ELROBOT_CAMERA_WIDTH": "640"}, "or neither"),
        ({"ELROBOT_POLICY_DEVICE": "mps"}, "auto, cpu, cuda"),
        ({"ELROBOT_RETURN_SECONDS": "9"}, "above the maximum 8"),
        ({"ELROBOT_MAX_RELATIVE_TARGET": "0"}, "below the minimum"),
        ({"ELROBOT_FOLLOWER_PORT": "COM3"}, "device path"),
        ({"ELROBOT_TASK": " "}, "ELROBOT_TASK is empty"),
        ({"ELROBOT_MODEL_REVISION": "a b"}, "not a branch"),
        ({"ELROBOT_CALIBRATE": "leader"}, "must be follower, or off"),
        ({"ELROBOT_CAMERAS": "/dev/video0"}, "has no name"),
    ],
)
def test_policy_rejects(env, message):
    with pytest.raises(SettingsError, match=message):
        policy_settings.load({**MODEL, **env}, is_dir=no_dirs)


def test_policy_buttons():
    runner = run_policy.Runner(policy_settings.load(MODEL, is_dir=no_dirs))
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


def runner_with(arm_, env=None):
    settings = policy_settings.load({**MODEL, "ELROBOT_RETURN_SECONDS": "0.1", **(env or {})}, is_dir=no_dirs)
    runner = run_policy.Runner(settings)
    runner.loaded = run_policy.LoadedPolicy(None, None, None, "act", "cpu", {}, {})
    runner.make_follower = lambda: arm_
    return runner


def test_a_failing_policy_step_still_returns_the_arm_and_turns_torque_off():
    start = {joint: 10.0 for joint in JOINTS}
    arm_ = ConnectableArm(positions=start)
    runner = runner_with(arm_)

    def policy_moves_then_fails(robot):
        robot.send_action({"shoulder_yaw.pos": 80.0, "gripper.pos": 90.0})
        raise ConnectionError("Incorrect status packet!")

    runner.control_loop = policy_moves_then_fails
    runner.run_once()
    assert arm_.bus.goals[0] == {"shoulder_yaw": 80.0, "gripper": 90.0}
    assert arm_.bus.goals[-1] == start
    assert not arm_.bus.torque and not arm_.bus.is_connected
    assert runner.state == "ready" and "torque off" in runner.detail
    assert runner.error == "ConnectionError: Incorrect status packet!"


def test_stop_before_the_policy_runs_only_releases_the_arm():
    arm_ = ConnectableArm()
    runner = runner_with(arm_)
    runner.stop()

    def must_not_run(_robot):
        raise AssertionError("the policy ran after Stop")

    runner.control_loop = must_not_run
    runner.run_once()
    assert not arm_.bus.torque and runner.state == "ready" and runner.error is None


def test_a_follower_that_cannot_be_built_leaves_the_runner_ready():
    runner = runner_with(None)

    def broken():
        raise common.SettingsError("the servos on /dev/ttyACM1 run at 5.0 V")

    runner.make_follower = broken
    runner.run_once()
    assert runner.state == "ready" and runner.detail == "stopped: the arm was not connected"
    assert "5.0 V" in runner.error


class FakeFeature:
    def __init__(self, kind, shape):
        self.type = type("FeatureType", (), {"value": kind})()
        self.shape = shape


def test_feature_table_reads_lerobot_policy_features():
    table = run_policy.feature_table({"observation.images.arm": FakeFeature("VISUAL", [3, 480, 640])})
    assert table == {"observation.images.arm": ("VISUAL", (3, 480, 640))}


# ── template files ───────────────────────────────────────────────────────────

ACTION_RE = re.compile(r"{{(.*?)}}", re.DOTALL)
TEMPLATES = [RECORD, POLICY]
IDS = ["record", "policy"]


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
        names.update(re.findall(r'"((?:ELROBOT|HF)_[A-Z_]+)"', source.read_text()))
    return names


@pytest.mark.parametrize("template", TEMPLATES, ids=IDS)
def test_template_actions_are_declared_and_python_has_none(template):
    declared = set(defaults(template))
    for path in template_files(template):
        used = {match.strip() for match in ACTION_RE.findall(path.read_text())}
        assert {name.removeprefix(".") for name in used} <= declared, path
        if path.suffix in (".py", ".md") or path.parent.name == "licenses":
            # wendy init renders text files as Go templates, so "{{" in code, docs or a license would break init.
            assert "{{" not in path.read_text() or path.name == "README.md", path


@pytest.mark.parametrize("template", TEMPLATES, ids=IDS)
def test_template_manifest(template):
    data = manifest(template)
    assert data["name"] == template.name
    names = [variable["name"] for variable in data["variables"]]
    assert names[:2] == ["APP_ID", "PORT"] and "FOLLOWER_SERIAL_DEVICE" in names and "CALIBRATION_VOLUME" in names
    assert defaults(template)["FOLLOWER_SERIAL_DEVICE"] == "ttyACM1"
    assert defaults(template)["CALIBRATION_VOLUME"] == "elrobot-calibration"


def test_record_wendy_json():
    wendy = json.loads(render((RECORD / "wendy.json").read_text(), {**defaults(RECORD), "APP_ID": "rec"}))
    assert wendy["appId"] == "rec" and wendy["platform"] == "linux"
    assert {"type": "serial", "device": "ttyACM1"} in wendy["entitlements"]
    assert {"type": "serial", "device": "ttyACM0"} in wendy["entitlements"]
    assert {"type": "camera"} in wendy["entitlements"]
    assert {"type": "persist", "name": "rec-data", "path": "/data"} in wendy["entitlements"]
    assert {"type": "persist", "name": "elrobot-calibration", "path": "/calibration"} in wendy["entitlements"]
    assert wendy["readiness"]["tcpSocket"] == {"port": 8094}
    assert '"gpu"' not in (RECORD / "wendy.json").read_text()


def test_policy_wendy_json():
    wendy = json.loads(render((POLICY / "wendy.json").read_text(), {**defaults(POLICY), "APP_ID": "pol"}))
    types = [entitlement["type"] for entitlement in wendy["entitlements"]]
    assert types.count("serial") == 1 and "camera" in types and "gpu" in types
    assert {"type": "serial", "device": "ttyACM1"} in wendy["entitlements"]
    assert {"type": "persist", "name": "pol-data", "path": "/data"} in wendy["entitlements"]
    assert {"type": "persist", "name": "elrobot-calibration", "path": "/calibration"} in wendy["entitlements"]
    assert wendy["readiness"]["tcpSocket"] == {"port": 8095}


def test_dockerfile_defaults_are_valid_settings():
    env = dockerfile_env(RECORD)
    s = record_settings.load(env)
    assert s.follower_port == "/dev/ttyACM1" and s.leader_port == "/dev/ttyACM0" and s.http_port == 8094
    assert s.cameras == {"arm": "/dev/video0", "table": "/dev/video2"}
    assert env["HF_LEROBOT_HOME"] == "/data/lerobot"
    env = dockerfile_env(POLICY)
    assert "ELROBOT_MODEL_PATH" not in env  # no public ElRobot model to default to
    s = policy_settings.load({**env, **MODEL}, is_dir=no_dirs)
    assert s.http_port == 8095 and s.follower_port == "/dev/ttyACM1"
    assert env["HF_HOME"].startswith("/data/") and env["TORCH_HOME"].startswith("/data/")


@pytest.mark.parametrize("template", TEMPLATES, ids=IDS)
def test_dockerfile_copies_every_source_file_and_the_license(template):
    dockerfile = (template / "Dockerfile").read_text()
    copied = set()
    for line in dockerfile.splitlines():
        if line.startswith("COPY "):
            copied.update(line.split()[1:-1])
    sources = {p.name for p in template.glob("*.py")}
    assert sources <= copied, f"the image would miss {sorted(sources - copied)}"
    assert "licenses/" in copied and "UPSTREAM.md" in copied


@pytest.mark.parametrize(
    "template, sources",
    [
        (RECORD, ("record_settings.py", "record.py", "elrobot_common.py")),
        (POLICY, ("policy_settings.py", "run_policy.py", "elrobot_common.py")),
    ],
    ids=IDS,
)
def test_every_setting_is_documented_and_every_dockerfile_setting_is_read(template, sources):
    read = env_names_read_by(*(template / name for name in sources)) | {"HF_TOKEN"}
    readme = (template / "README.md").read_text()
    undocumented = sorted(name for name in read if f"`{name}`" not in readme)
    assert not undocumented, f"{template.name}/README.md does not document {undocumented}"
    library = {"HF_LEROBOT_HOME", "HF_HOME", "TORCH_HOME", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE"}
    unread = sorted(set(dockerfile_env(template)) - read - library)
    assert not unread, f"{template.name}/Dockerfile sets {unread}, which the app never reads"


# Every servo register the code can write, by where it is written.
WRITTEN_REGISTERS = {
    "Goal_Position", "Torque_Enable", "Lock",  # every run (RAM)
    "Homing_Offset", "Min_Position_Limit", "Max_Position_Limit",  # ELROBOT_WRITE_CALIBRATION
    "Return_Delay_Time", "Maximum_Acceleration", "Acceleration", "Phase",  # LeRobot's configure_motors()
}


@pytest.mark.parametrize("template", TEMPLATES, ids=IDS)
def test_readme_names_every_register_the_app_can_write(template):
    source = (template / "elrobot_arms.py").read_text()
    written = set(re.findall(r'\.write\("(\w+)"', source)) | WRITTEN_REGISTERS
    readme = (template / "README.md").read_text()
    missing = sorted(name for name in written if f"`{name}`" not in readme)
    assert not missing, f"{template.name}/README.md does not say when the app writes {missing}"
    assert "`ELROBOT_WRITE_CALIBRATION`" in readme and "`ELROBOT_CONFIGURE_SERVOS`" in readme


@pytest.mark.parametrize("template", TEMPLATES, ids=IDS)
def test_lerobot_and_python_versions_match_the_training_image(template):
    requirements = (template / "requirements.txt").read_text()
    assert re.search(r"^lerobot\[[a-z,]+\]==0\.6\.1$", requirements, re.MULTILINE)
    dockerfile = (template / "Dockerfile").read_text()
    assert "FROM python:3.12-slim-bookworm@sha256:" in dockerfile
    assert "torch==2.11.0 torchvision==0.26.0" in dockerfile


def test_policy_image_needs_no_dataset_extra():
    assert "dataset" not in (POLICY / "requirements.txt").read_text().split("lerobot[", 1)[1].split("]")[0]
    assert "smolvla" in (POLICY / "requirements.txt").read_text()


def test_catalog_lists_both_templates():
    meta = json.loads((ROOT / "meta.json").read_text())
    entries = {template["name"]: template for template in meta["templates"]}
    for template in TEMPLATES:
        assert entries[template.name]["languages"] == ["python"]
        assert "ElRobot" in entries[template.name]["description"]
        assert f"[Python](python/{template.name}/)" in (ROOT / "README.md").read_text()
