"""Helpers shared by the elrobot-record and elrobot-policy templates.

Both templates carry a byte-identical copy of this file, because `wendy init`
copies one template directory on its own; a repository test keeps the copies
equal. Module level uses the standard library only, so the settings and safety
code can be unit-tested without LeRobot. The functions that open a serial port
or a camera import LeRobot inside their bodies.

Servo writes. Unless a setting asks for more, the apps write only RAM
registers of the arms: Goal_Position, and Torque_Enable with Lock (LeRobot
writes them together). Calibration registers (Homing_Offset,
Min_Position_Limit, Max_Position_Limit) are written only with
ELROBOT_WRITE_CALIBRATION=true, and the upstream integration's configure()
(PID gains, gripper torque and current limits, LeRobot's bus defaults) runs
only with ELROBOT_CONFIGURE_SERVOS=true. The dry run and the calibration mode
never write to the servos. The README has the full table.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

log = logging.getLogger(__name__)

# The ElRobot joints in motor order (servo IDs 1 to 8), with the names of the
# community LeRobot integration (dreamerarun/elrobot_teleoperation). Datasets
# and policies use this order, so it is fixed rather than configurable.
JOINTS = (
    "shoulder_yaw",
    "shoulder_pitch",
    "elbow_yaw",
    "elbow_pitch",
    "wrist_pitch",
    "wrist_roll",
    "wrist_yaw",
    "gripper",
)
SERVO_IDS = {joint: index + 1 for index, joint in enumerate(JOINTS)}
GRIPPER = "gripper"
CALIBRATION_FIELDS = ("id", "drive_mode", "homing_offset", "range_min", "range_max")
MAX_TICK = 4095  # STS3215 encoder: 0 to 4095 ticks per turn
TICKS_PER_TURN = 4096
# LeRobot's half-turn homing (set_half_turn_homings) makes the home pose read this.
HALF_TURN = 2047
# What LeRobot's DEGREES normalization divides by: degrees = (ticks - mid) * 360 / 4095.
DEGREE_TICKS = MAX_TICK / 360.0

# The follower's joint limits in degrees, from norma-core's URDF
# (hardware/elrobot/simulation/elrobot_follower.urdf at commit
# 0d17bb300d712eeb5437d41ddb95ce380b5384e6; rev_motor_01 to rev_motor_08).
# The URDF does not say which encoder reading is its zero pose, so the apps
# use these spans to check a calibration's ranges, not as absolute limits.
# There is no URDF for the leader.
URDF_LIMITS_DEG = {
    "shoulder_yaw": (-88.9, 88.9),
    "shoulder_pitch": (-92.4, 92.4),
    "elbow_yaw": (-100.9, 100.9),
    "elbow_pitch": (-100.5, 100.5),
    "wrist_pitch": (-150.1, 186.3),
    "wrist_roll": (-78.9, 101.1),
    "wrist_yaw": (-183.4, 156.6),
    "gripper": (0.0, 126.2),
}
# Every URDF joint has velocity="4.71" rad/s, about 270 degrees a second.
URDF_VELOCITY_DEG_S = 270.0
# A calibrated range may exceed the URDF span by this much (the sweep's slack).
SPAN_TOLERANCE_DEG = 15.0
# Before torque goes on, every follower joint must be within its calibrated
# range plus this margin (5 degrees), or the hold would not be where it is.
RANGE_TOLERANCE_TICKS = 57
# The follower runs on 12 V and the leader on 5 to 7.4 V (norma-core BOM), so
# the supply voltage tells the arms apart. Present_Voltage is in 0.1 V.
FOLLOWER_MIN_VOLTS = 9.5
LEADER_MAX_VOLTS = 9.0

# The recorder writes this task sentence into the dataset unless ELROBOT_TASK
# is set, and the policy runner sends the same sentence to language-conditioned
# policies (SmolVLA), so a model trained on a default recording gets its task.
DEFAULT_TASK = "Pick up the cube and place it in the box."

TRUE_WORDS = {"1", "true", "yes", "on"}
FALSE_WORDS = {"0", "false", "no", "off"}
CAMERA_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class SettingsError(ValueError):
    """A setting is missing or invalid; the message says which and how to fix it."""


# ── environment parsing ──────────────────────────────────────────────────────


def env_str(env, name, default=""):
    value = env.get(name)
    if value is None:
        return default
    return value.strip()


def env_bool(env, name, default):
    raw = env_str(env, name)
    if raw == "":
        return default
    word = raw.lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise SettingsError(f"{name}={raw!r} is not a yes/no value; use true or false")


def env_int(env, name, default, minimum=None, maximum=None):
    raw = env_str(env, name)
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name}={raw!r} is not a whole number") from None
    _check_range(name, value, minimum, maximum)
    return value


def env_float(env, name, default, minimum=None, maximum=None):
    raw = env_str(env, name)
    if raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"{name}={raw!r} is not a number") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise SettingsError(f"{name}={raw!r} is not a finite number")
    _check_range(name, value, minimum, maximum)
    return value


def _check_range(name, value, minimum, maximum):
    if minimum is not None and value < minimum:
        raise SettingsError(f"{name}={value} is below the minimum {minimum}")
    if maximum is not None and value > maximum:
        raise SettingsError(f"{name}={value} is above the maximum {maximum}")


def serial_port(env, name, default):
    """A serial device path such as /dev/ttyACM1."""
    port = env_str(env, name, default)
    if not port.startswith("/dev/"):
        raise SettingsError(f"{name}={port!r} must be a device path such as /dev/ttyACM1")
    return port


def camera_fourcc(env, name="ELROBOT_CAMERA_FOURCC", default="MJPG"):
    """A 4-character pixel format, or None for the camera's default (set the variable to `auto`)."""
    value = env_str(env, name, default)
    if value.lower() in ("", "auto"):
        return None
    if len(value) != 4:
        raise SettingsError(f"{name}={value!r} must be 4 characters, such as MJPG or YUYV, or auto")
    return value


def parse_cameras(text, variable="ELROBOT_CAMERAS"):
    """Parse comma-separated `name=/dev/videoN` entries into {name: device}.

    Every camera is named: the name becomes observation.images.<name> in the
    dataset, and nothing here guesses which device is the arm camera and which
    the table camera.
    """
    cameras = {}
    for item in (part.strip() for part in text.split(",")):
        if not item:
            continue
        if "=" not in item:
            raise SettingsError(
                f"{variable}: {item!r} has no name; write name=device, for example arm=/dev/video0,table=/dev/video2"
            )
        name, device = (piece.strip() for piece in item.split("=", 1))
        if not CAMERA_NAME.match(name):
            raise SettingsError(
                f"{variable}: camera name {name!r} must be lowercase letters, digits and _, "
                "starting with a letter (it becomes observation.images.<name>)"
            )
        if not device:
            raise SettingsError(f"{variable}: {item!r} has no device")
        if not (device.isdigit() or device.startswith("/dev/")):
            raise SettingsError(f"{variable}: {device!r} must be a device path such as /dev/video0")
        if name in cameras:
            raise SettingsError(f"{variable}: camera {name} is listed twice")
        if device in cameras.values():
            raise SettingsError(f"{variable}: {device} is listed for two cameras")
        cameras[name] = device
    if not cameras:
        raise SettingsError(f"{variable} is empty; set it to name=/dev/videoN, for example arm=/dev/video0")
    return cameras


def camera_index_or_path(device):
    """LeRobot's OpenCV camera takes an integer index or a device path."""
    return int(device) if device.isdigit() else Path(device)


def calibrate_role(env, roles):
    """ELROBOT_CALIBRATE: which arm the calibration mode is for, or None (normal operation)."""
    raw = env_str(env, "ELROBOT_CALIBRATE").lower()
    if raw in ("",) or raw in FALSE_WORDS:
        return None
    if raw not in roles:
        choices = " or ".join(roles)
        raise SettingsError(f"ELROBOT_CALIBRATE={raw!r} must be {choices}, or off")
    return raw


def max_relative_target(env, fps, name="ELROBOT_MAX_RELATIVE_TARGET"):
    """LeRobot's max_relative_target: the largest move of one joint per command, in joint units.

    Unset, it is the URDF's joint velocity limit divided by the command rate
    (9 degrees at 30 commands a second). `none` turns the cap off.
    """
    raw = env_str(env, name)
    if raw == "":
        return round(URDF_VELOCITY_DEG_S / fps, 1)
    if raw.lower() in ("none", "off"):
        return None
    return env_float({name: raw}, name, None, minimum=0.1)


# ── calibration files ────────────────────────────────────────────────────────


@dataclass
class CalibrationFile:
    path: Path
    data: dict
    warnings: list = field(default_factory=list)


def load_calibration(path, role, variable):
    """Read and check a LeRobot calibration JSON for the ElRobot `role` arm.

    `role` is "follower" or "leader"; `variable` is the env var that names the
    file. Raises SettingsError when the file cannot be used.
    """
    path = Path(path)
    if path.suffix != ".json":
        raise SettingsError(f"{variable}={path} must name a .json file")
    if not path.is_file():
        hint = ""
        if path.parent.is_dir():
            found = sorted(p.name for p in path.parent.glob("*.json"))
            hint = f" {path.parent} holds: {', '.join(found)}." if found else f" {path.parent} is empty."
        raise SettingsError(
            f"No {role} calibration at {path}.{hint} Calibrate the {role} arm first: run this app with "
            f"ELROBOT_CALIBRATE={role} (see the README), or set {variable} to the file."
        )
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SettingsError(f"{path} is not readable JSON: {exc}") from None
    return CalibrationFile(path, data, check_calibration(data, str(path), role))


def check_calibration(data, label, role):
    """Validate LeRobot's per-motor calibration shape for an ElRobot arm; return warnings.

    For the follower, each joint's calibrated range is also checked against
    the URDF: a range wider than the joint can travel means the sweep went
    wrong (the reading jumped across the encoder's 0/4095 boundary).
    """
    if not isinstance(data, dict):
        raise SettingsError(f"{label}: expected a JSON object with one entry per joint")
    missing = [joint for joint in JOINTS if joint not in data]
    if missing:
        raise SettingsError(f"{label}: no entry for {', '.join(missing)} (an ElRobot arm has 8 servos)")
    extra = sorted(set(data) - set(JOINTS))
    if extra:
        raise SettingsError(f"{label}: unknown joints {', '.join(extra)}; is this an SO-101 calibration?")
    warnings = []
    for joint in JOINTS:
        entry = data[joint]
        if not isinstance(entry, dict):
            raise SettingsError(f"{label}: {joint} must be an object")
        absent = [name for name in CALIBRATION_FIELDS if name not in entry]
        if absent:
            raise SettingsError(f"{label}: {joint} has no {', '.join(absent)}")
        values = {}
        for name in CALIBRATION_FIELDS:
            value = entry[name]
            if isinstance(value, bool) or not isinstance(value, int):
                raise SettingsError(f"{label}: {joint}.{name} must be a whole number, got {value!r}")
            values[name] = value
        if values["id"] != SERVO_IDS[joint]:
            raise SettingsError(f"{label}: {joint} has servo id {values['id']}, expected {SERVO_IDS[joint]}")
        if values["drive_mode"] not in (0, 1):
            raise SettingsError(f"{label}: {joint}.drive_mode must be 0 or 1")
        if not -2047 <= values["homing_offset"] <= 2047:
            raise SettingsError(f"{label}: {joint}.homing_offset {values['homing_offset']} is outside -2047..2047")
        low, high = values["range_min"], values["range_max"]
        if low >= high:
            raise SettingsError(
                f"{label}: {joint} range_min {low} is not below range_max {high}; sweep the joint again and save"
            )
        if low < 0 or high > MAX_TICK:
            warnings.append(
                f"{joint}: range {low}..{high} is outside 0..{MAX_TICK}; calibrate again with ELROBOT_CALIBRATE={role}"
            )
        if role == "follower":
            warning = _check_urdf_span(label, joint, low, high)
            if warning:
                warnings.append(warning)
    return warnings


def _check_urdf_span(label, joint, low, high):
    lower, upper = URDF_LIMITS_DEG[joint]
    travel = upper - lower
    span = (high - low) / DEGREE_TICKS
    if span <= travel + SPAN_TOLERANCE_DEG:
        return None
    if joint == "wrist_roll" and (low, high) == (0, MAX_TICK):
        # lerobot-calibrate with the upstream integration saves wrist_roll as a full turn.
        return (
            "wrist_roll: the range is the full turn 0..4095 (upstream lerobot-calibrate's convention), "
            f"so the app cannot limit it; the URDF gives it {lower}..{upper} degrees"
        )
    raise SettingsError(
        f"{label}: {joint} spans {span:.0f} degrees, but the follower's {joint} travels {travel:.1f} "
        "(norma-core URDF). The sweep went wrong, probably across the encoder's 0/4095 boundary; "
        "calibrate again (set home in the middle of the joint's travel first)"
    )


def _field(entry, name):
    return entry[name] if isinstance(entry, dict) else getattr(entry, name)


def calibration_differences(on_servos, in_file):
    """Compare the servos' calibration registers with a calibration file.

    Both map joint -> LeRobot MotorCalibration (or a dict with its fields).
    Returns (offsets, limits): lists of joints whose Homing_Offset, or whose
    Min/Max_Position_Limit, differ.
    """
    offsets, limits = [], []
    for joint in JOINTS:
        servo, file = on_servos[joint], in_file[joint]
        if _field(servo, "homing_offset") != _field(file, "homing_offset"):
            offsets.append(joint)
        if (_field(servo, "range_min"), _field(servo, "range_max")) != (
            _field(file, "range_min"),
            _field(file, "range_max"),
        ):
            limits.append(joint)
    return offsets, limits


def goal_limits(calibration, use_degrees):
    """Each joint's calibrated range in the units LeRobot reports: {joint: (low, high)}.

    In degrees, LeRobot's normalization has no bounds, so these are the only
    software limit (the servos' own position limits stay 0..4095 unless
    ELROBOT_WRITE_CALIBRATION writes the file's ranges). In -100..100 and
    0..100 units, LeRobot already clamps to the same range.
    """
    limits = {}
    for joint in JOINTS:
        if joint == GRIPPER:
            limits[joint] = (0.0, 100.0)
        elif use_degrees:
            low, high = _field(calibration[joint], "range_min"), _field(calibration[joint], "range_max")
            mid = (low + high) / 2
            limits[joint] = ((low - mid) / DEGREE_TICKS, (high - mid) / DEGREE_TICKS)
        else:
            limits[joint] = (-100.0, 100.0)
    return limits


def clamp_goals(values, limits):
    """Clamp `<joint>.pos` goals to `limits`; other keys pass through unchanged."""
    if not limits:
        return dict(values)
    clamped = {}
    for key, value in values.items():
        joint = key.removesuffix(".pos")
        if key.endswith(".pos") and joint in limits:
            low, high = limits[joint]
            value = min(high, max(low, value))
        clamped[key] = value
    return clamped


# ── checks before torque goes on (reads only) ────────────────────────────────


def check_supply(bus, role, port):
    """Refuse an arm whose supply voltage says it is the other arm (swapped ports)."""
    volts = {joint: value / 10 for joint, value in bus.sync_read("Present_Voltage", normalize=False, num_retry=2).items()}
    lowest, highest = min(volts.values()), max(volts.values())
    if role == "follower" and lowest < FOLLOWER_MIN_VOLTS:
        raise SettingsError(
            f"the servos on {port} run at {lowest:.1f} V, but the ElRobot follower runs on 12 V: this looks like "
            "the leader, or the follower's 12 V supply is off. Check ELROBOT_FOLLOWER_PORT and "
            "ELROBOT_LEADER_PORT (ls -l /dev/serial/by-id/), or set ELROBOT_CHECK_VOLTAGE=false"
        )
    if role == "leader" and highest > LEADER_MAX_VOLTS:
        raise SettingsError(
            f"the servos on {port} run at {highest:.1f} V, but the ElRobot leader runs on 5 to 7.4 V: this looks "
            "like the follower. Check ELROBOT_FOLLOWER_PORT and ELROBOT_LEADER_PORT (ls -l /dev/serial/by-id/), "
            "or set ELROBOT_CHECK_VOLTAGE=false"
        )
    return f"{lowest:.1f} to {highest:.1f} V"


def check_position_mode(bus):
    modes = bus.sync_read("Operating_Mode", normalize=False, num_retry=2)
    other = {joint: mode for joint, mode in modes.items() if mode != 0}
    if other:
        listed = ", ".join(f"{joint} is in mode {mode}" for joint, mode in other.items())
        raise SettingsError(
            f"{listed}, not position mode (0). Set ELROBOT_CONFIGURE_SERVOS=true to write position mode "
            "(with the integration's other settings, see the README)"
        )


def check_within_range(bus, calibration, tolerance=RANGE_TOLERANCE_TICKS):
    """Refuse to turn torque on while a joint is outside its calibrated range."""
    present = bus.sync_read("Present_Position", normalize=False, num_retry=2)
    outside = []
    for joint in JOINTS:
        low, high = _field(calibration[joint], "range_min"), _field(calibration[joint], "range_max")
        value = present[joint]
        if value < low - tolerance or value > high + tolerance:
            outside.append(f"{joint} reads {value}, calibrated {low}..{high}")
    if outside:
        raise SettingsError(
            "joints outside their calibrated range: " + "; ".join(outside) + ". Torque is still off: move them "
            "into range by hand, or calibrate the arm again"
        )


def hold_present_position(bus):
    """Make each servo's goal its present position (raw ticks), so turning torque on moves nothing."""
    present = bus.sync_read("Present_Position", normalize=False, num_retry=2)
    bus.sync_write("Goal_Position", present, normalize=False, num_retry=2)


def release_leader_torque(bus):
    """Turn torque off on leader servos that have it on, so the leader can be moved by hand."""
    torque = bus.sync_read("Torque_Enable", normalize=False, num_retry=2)
    held = [joint for joint, value in torque.items() if value]
    if held:
        log.info("leader servos with torque on: %s; turning it off", ", ".join(held))
        bus.disable_torque(held)
    return held


# ── LeRobot arm classes for a container: no prompts, opt-in writes ───────────


@dataclass(frozen=True)
class ArmOptions:
    role: str  # "follower" or "leader"
    write_calibration: bool = False
    configure_servos: bool = False
    check_voltage: bool = True


def wendy_arm(cls, options):
    """Subclass the vendored ElRobot follower or leader for a Wendy app.

    LeRobot's connect() calls calibrate() when the servos do not hold the
    calibration file's values, and the integration's calibrate() asks on stdin.
    Here calibrate() never prompts:

    - With ELROBOT_WRITE_CALIBRATION=true, it writes the file (Homing_Offset,
      Min/Max_Position_Limit) with torque off, as LeRobot does.
    - Otherwise it writes nothing. If only the position limits differ (a file
      made by this app's calibration mode keeps the servos' homing offsets),
      the app goes on and limits goals in software. If a homing offset
      differs, positions would mean something else, so connect() fails.

    configure() checks the supply voltage, position mode and (follower) that
    every joint is inside its calibrated range, then sets each follower
    servo's goal to its present position and turns torque on, so the arm holds
    still. The integration's own configure() (PID 16/0/32, gripper limits,
    LeRobot's bus defaults) runs only with ELROBOT_CONFIGURE_SERVOS=true. The
    follower's send_action() clamps goals to the calibrated ranges.
    """

    class WendyArm(cls):
        supply = None
        goal_limits = None

        def calibrate(self):
            if not self.calibration:
                raise SettingsError(f"no calibration file at {self.calibration_fpath}")
            self._check_supply()
            offsets, limits = calibration_differences(self.bus.read_calibration(), self.calibration)
            if options.write_calibration:
                log.warning(
                    "ELROBOT_WRITE_CALIBRATION=true: writing %s to the %s servos with torque off "
                    "(Homing_Offset differs for %s; position limits differ for %s)",
                    self.calibration_fpath, options.role, ", ".join(offsets) or "none", ", ".join(limits) or "none",
                )
                self.bus.disable_torque()
                self.bus.write_calibration(self.calibration)
                return
            if offsets:
                raise SettingsError(
                    f"the {options.role} servos' homing offsets differ from {self.calibration_fpath} for "
                    f"{', '.join(offsets)}, so their positions would be misread. Calibrate this arm again with "
                    f"ELROBOT_CALIBRATE={options.role}, or set ELROBOT_WRITE_CALIBRATION=true to write the file's "
                    "offsets and ranges to the servos (this replaces the calibration they hold now)"
                )
            log.info(
                "%s: servo position limits differ from %s for %s; not written (ELROBOT_WRITE_CALIBRATION is off), "
                "goals are limited in software",
                options.role, self.calibration_fpath.name, ", ".join(limits),
            )

        def configure(self):
            self._check_supply()
            if options.role == "follower":
                if not options.configure_servos:
                    check_position_mode(self.bus)
                check_within_range(self.bus, self.calibration)
                self.goal_limits = goal_limits(self.calibration, self.config.use_degrees)
                hold_present_position(self.bus)
                if options.configure_servos:
                    super().configure()  # the integration's writes, then torque on
                else:
                    self.bus.enable_torque()
            elif options.configure_servos:
                super().configure()
            else:
                release_leader_torque(self.bus)

        def _check_supply(self):
            if options.check_voltage and self.supply is None:
                self.supply = check_supply(self.bus, options.role, self.config.port)

    if options.role == "follower":

        def send_action(self, action):
            return cls.send_action(self, clamp_goals(action, self.goal_limits))

        WendyArm.send_action = send_action

    WendyArm.__name__ = WendyArm.__qualname__ = f"{cls.__name__}Wendy"
    return WendyArm


# ── stopping the arm safely ──────────────────────────────────────────────────


def release(device):
    """Disconnect a robot or leader arm with torque off, even if connect() failed halfway.

    Never raises: this runs on shutdown paths. Returns True when the servo bus
    is closed (LeRobot turns torque off before it closes the port).
    """
    try:
        if device.is_connected:
            device.disconnect()
    except Exception as exc:  # noqa: BLE001 - fall back to closing the bus directly
        log.warning("could not disconnect %s: %s", device, first_line(exc))
    bus = device.bus
    try:
        if bus.is_connected:
            bus.disconnect(disable_torque=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not turn torque off on %s: %s", device, first_line(exc))
    for camera in getattr(device, "cameras", {}).values():
        try:
            if camera.is_connected:
                camera.disconnect()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not close a camera of %s: %s", device, first_line(exc))
    try:
        return not bus.is_connected
    except Exception:  # noqa: BLE001
        return False


def present_pose(robot):
    """The arm's joint positions as `<joint>.pos` values, read from the servo bus only.

    The safety paths read and write the servo bus directly, rather than through
    robot.get_observation() and robot.send_action(), which refuse to run once a
    camera has failed.
    """
    values = robot.bus.sync_read("Present_Position", num_retry=2)
    return {f"{motor}.pos": float(value) for motor, value in values.items()}


def move_seconds(current, target, speed=45.0, minimum=2.0, maximum=8.0):
    """How long a smooth move from `current` to `target` takes: the farthest joint at `speed` units/s."""
    distances = [abs(target[key] - current[key]) for key in target if key in current]
    if not distances:
        return minimum
    return min(maximum, max(minimum, max(distances) / speed))


def move_smoothly(robot, target, seconds, fps=30, sleep=time.sleep, should_stop=None):
    """Move the follower from where it is to `target` (`<joint>.pos` values) over `seconds`.

    Writes linearly interpolated goal positions at `fps`, like the return to
    the initial position in lerobot-rollout, through the same servo bus write
    LeRobot's send_action() makes. The target is clamped to the arm's
    calibrated ranges (robot.goal_limits). Returns the number of goals sent.
    `should_stop` (a callable) ends the move early.
    """
    if seconds <= 0 or not target:
        return 0
    target = clamp_goals(target, getattr(robot, "goal_limits", None))
    current = present_pose(robot)
    keys = [key for key in target if key in current]
    steps = max(1, int(seconds * fps))
    for step in range(1, steps + 1):
        if should_stop is not None and should_stop():
            return step - 1
        t = step / steps
        goal = {key.removesuffix(".pos"): current[key] + (target[key] - current[key]) * t for key in keys}
        robot.bus.sync_write("Goal_Position", goal, num_retry=2)
        sleep(1.0 / fps)
    return steps


def safe_stop(robot, rest_pose, seconds, fps=30, sleep=time.sleep):
    """Bring the follower back to `rest_pose`, then turn torque off and close the port.

    Torque goes off even when the move fails, so the arm is never left driven
    by a stopped program. Returns a short report for the log and the page.
    """
    moved = "not moved"
    try:
        if robot.bus.is_connected and rest_pose and seconds > 0:
            move_smoothly(robot, rest_pose, seconds, fps=fps, sleep=sleep)
            moved = "returned to its start pose"
    except Exception as exc:  # noqa: BLE001 - the release below must still happen
        moved = f"could not return to its start pose ({first_line(exc)})"
    released = release(robot)
    return f"arm {moved}; " + ("torque off" if released else "torque state unknown, see the log")


# ── browser control page and JSON API ────────────────────────────────────────


class ControlServer:
    """A small HTTP server: GET / (page), GET /api/status, GET /camera/<name>.jpg, POST /api/<action>.

    POSTs from another website are refused, so a page on the internet cannot
    drive the arm through the user's browser.
    """

    def __init__(self, port, page, status, actions, images=None):
        self.port = port
        self.page = page.encode()
        self.status = status
        self.actions = actions
        self.images = images
        self.httpd = None

    def start(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _send(self, code, body, content_type="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/":
                    self._send(200, server.page, "text/html; charset=utf-8")
                elif path in ("/api/status", "/health"):
                    self._send(200, server.status())
                elif path.startswith("/camera/") and path.endswith(".jpg") and server.images is not None:
                    image = server.images(unquote(path.removeprefix("/camera/").removesuffix(".jpg")))
                    if image is None:
                        self._send(404, {"error": "no picture from that camera yet"})
                    else:
                        self._send(200, image, "image/jpeg")
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                path = urlsplit(self.path).path
                if not same_origin(self.headers):
                    self._send(403, {"error": "cross-origin control is disabled"})
                    return
                name = path.removeprefix("/api/")
                action = server.actions.get(name) if path.startswith("/api/") else None
                if action is None:
                    self._send(404, {"error": "not found"})
                    return
                try:
                    result = action()
                except SettingsError as exc:
                    self._send(409, {"error": str(exc)})
                    return
                except Exception as exc:  # noqa: BLE001 - for example a servo read that failed
                    log.warning("%s failed: %s", name, first_line(exc))
                    self._send(500, {"error": f"{type(exc).__name__}: {first_line(exc)}"})
                    return
                self._send(200, {"result": result, "status": server.status()})

        self.httpd = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, name="http", daemon=True).start()
        return self


def same_origin(headers):
    origin = headers.get("Origin")
    if origin and urlsplit(origin).netloc != headers.get("Host"):
        return False
    return headers.get("Sec-Fetch-Site") != "cross-site"


class Snapshots:
    """The latest JPEG from each camera, so the page shows which device is which camera."""

    def __init__(self):
        self.lock = threading.Lock()
        self.images = {}
        self.taken = {}

    def put(self, name, frame_rgb):
        try:
            image = jpeg(frame_rgb)
        except Exception as exc:  # noqa: BLE001 - a missing picture must not stop the app
            log.warning("could not encode a picture from camera %s: %s", name, first_line(exc))
            return
        with self.lock:
            self.images[name] = image
            self.taken[name] = time.strftime("%H:%M:%S")

    def get(self, name):
        with self.lock:
            return self.images.get(name)

    def listing(self, cameras):
        with self.lock:
            return [
                {"name": name, "device": device, "taken": self.taken.get(name)} for name, device in cameras.items()
            ]

    def take(self, robot):
        """Grab the latest frame of each of the robot's connected cameras."""
        for name, camera in getattr(robot, "cameras", {}).items():
            try:
                self.put(name, camera.read_latest())
            except Exception as exc:  # noqa: BLE001
                log.warning("no picture from camera %s: %s", name, first_line(exc))


def jpeg(frame_rgb, max_width=480):
    import cv2

    height, width = frame_rgb.shape[:2]
    if width > max_width:
        frame_rgb = cv2.resize(frame_rgb, (max_width, round(height * max_width / width)))
    ok, data = cv2.imencode(".jpg", cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR))
    if not ok:
        raise ValueError("JPEG encoding failed")
    return data.tobytes()


CAMERA_SCRIPT = """
function cameraFigures(list){
  return (list || []).map(c => '<figure><img src="/camera/' + encodeURIComponent(c.name) + '.jpg?t=' +
    encodeURIComponent(c.taken || '') + '" alt=""><figcaption>' + c.name + ' = ' + c.device +
    (c.taken ? ' (' + c.taken + ')' : ' (no picture yet)') + '</figcaption></figure>').join('');
}
"""
CAMERA_STYLE = "figure{display:inline-block;margin:6px 10px 6px 0}figure img{max-width:320px;border:1px solid #333}"


# ── read-only hardware probes (dry run) ──────────────────────────────────────

# Registers the dry run reads from every servo (never writes).
PROBE_REGISTERS = (
    "Present_Position",
    "Present_Voltage",
    "Torque_Enable",
    "Operating_Mode",
    "Homing_Offset",
    "Min_Position_Limit",
    "Max_Position_Limit",
    "P_Coefficient",
    "I_Coefficient",
    "D_Coefficient",
    "Max_Torque_Limit",
    "Protection_Current",
    "Overload_Torque",
)


def open_bus(port):
    """Open an ElRobot servo bus with LeRobot's Feetech driver. Reads only: ping and firmware check."""
    from lerobot.motors import Motor, MotorNormMode
    from lerobot.motors.feetech import FeetechMotorsBus

    motors = {joint: Motor(SERVO_IDS[joint], "sts3215", MotorNormMode.RANGE_M100_100) for joint in JOINTS}
    bus = FeetechMotorsBus(port=port, motors=motors, calibration=None)
    bus.connect(handshake=True)
    return bus


def read_registers(bus):
    """{register: {joint: raw value}} for PROBE_REGISTERS."""
    return {name: bus.sync_read(name, normalize=False, num_retry=2) for name in PROBE_REGISTERS}


def describe_arm(role, port, registers, calibration, options):
    """Turn a register read into report lines, and say what connecting would write. Pure."""
    volts = [value / 10 for value in registers["Present_Voltage"].values()]
    torque_on = [joint for joint in JOINTS if registers["Torque_Enable"][joint]]
    lines = [
        f"{role} {port}: 8/8 servos answered, {min(volts):.1f} to {max(volts):.1f} V, "
        + (f"torque ON for {', '.join(torque_on)}" if torque_on else "torque off")
    ]
    if options.check_voltage:
        if role == "follower" and min(volts) < FOLLOWER_MIN_VOLTS:
            lines.append(f"{role}: PROBLEM: below {FOLLOWER_MIN_VOLTS} V, so this is not the 12 V follower (swapped ports?)")
        if role == "leader" and max(volts) > LEADER_MAX_VOLTS:
            lines.append(f"{role}: PROBLEM: above {LEADER_MAX_VOLTS} V, so this is not the leader (swapped ports?)")
    for joint in JOINTS:
        value = {name: registers[name][joint] for name in PROBE_REGISTERS}
        lines.append(
            f"{role} {joint} (id {SERVO_IDS[joint]}): position {value['Present_Position']}, "
            f"homing offset {value['Homing_Offset']}, limits {value['Min_Position_Limit']}..{value['Max_Position_Limit']}, "
            f"mode {value['Operating_Mode']}, PID {value['P_Coefficient']}/{value['I_Coefficient']}/{value['D_Coefficient']}, "
            f"max torque {value['Max_Torque_Limit']}, protection current {value['Protection_Current']}, "
            f"overload torque {value['Overload_Torque']}"
        )
    lines.extend(planned_writes(role, registers, calibration, options))
    return lines


def planned_writes(role, registers, calibration, options):
    """What connecting this arm with these settings would do to its registers. Pure."""
    if calibration is None:
        return [f"{role}: no usable calibration file, so connecting would stop before any write"]
    data = calibration.data
    servo = {
        joint: {
            "homing_offset": registers["Homing_Offset"][joint],
            "range_min": registers["Min_Position_Limit"][joint],
            "range_max": registers["Max_Position_Limit"][joint],
        }
        for joint in JOINTS
    }
    offsets, limits = calibration_differences(servo, data)
    lines = []
    if offsets:
        lines.append(
            f"{role}: homing offsets differ from the file for {', '.join(offsets)}: "
            + ("connecting WOULD WRITE the file's Homing_Offset and position limits (ELROBOT_WRITE_CALIBRATION=true)"
               if options.write_calibration else "connecting would stop (ELROBOT_WRITE_CALIBRATION is off)")
        )
    else:
        lines.append(f"{role}: homing offsets match the file")
        if limits:
            lines.append(
                f"{role}: position limits differ from the file for {', '.join(limits)}: "
                + ("connecting WOULD WRITE them (ELROBOT_WRITE_CALIBRATION=true)" if options.write_calibration
                   else "left as they are; goals are limited in software")
            )
    modes = [joint for joint in JOINTS if registers["Operating_Mode"][joint] != 0]
    if options.configure_servos:
        lines.append(f"{role}: connecting WOULD WRITE the integration's settings (ELROBOT_CONFIGURE_SERVOS=true)")
    elif modes and role == "follower":
        lines.append(f"{role}: {', '.join(modes)} not in position mode: connecting would stop")
    if role == "follower":
        lines.append(f"{role}: at connect the app writes Goal_Position (the present position) and turns torque on")
    if role == "follower" and not offsets:
        outside = [
            joint for joint in JOINTS
            if not data[joint]["range_min"] - RANGE_TOLERANCE_TICKS
            <= registers["Present_Position"][joint]
            <= data[joint]["range_max"] + RANGE_TOLERANCE_TICKS
        ]
        if outside:
            lines.append(f"{role}: outside the calibrated range now: {', '.join(outside)}; Start would refuse")
    return lines


def probe_arm(port, role, calibration, options):
    """Ping one arm's servos and read their registers. Never writes to the bus. Returns report lines."""
    if not os.path.exists(port):
        return [f"{role} {port}: not present in the container"]
    try:
        bus = open_bus(port)
    except Exception as exc:  # noqa: BLE001 - report any failure as text
        hint = " Is its servo supply switched on?" if "Missing motor IDs" in str(exc) else ""
        return [f"{role} {port}: servos did not answer ({first_line(exc)}).{hint}"]
    try:
        registers = read_registers(bus)
    except Exception as exc:  # noqa: BLE001
        return [f"{role} {port}: servos answered, but a read failed ({first_line(exc)})"]
    finally:
        # disable_torque=False: closing the port must not write to the servos.
        bus.disconnect(disable_torque=False)
    return describe_arm(role, port, registers, calibration, options)


def probe_camera(name, device, width, height, fps, fourcc, snapshots=None):
    """Open a camera with LeRobot's OpenCV camera, read one frame and close it."""
    target = camera_index_or_path(device)
    if isinstance(target, Path) and not target.exists():
        return f"camera {name} ({device}): not present in the container"
    from lerobot.cameras.opencv import OpenCVCamera, OpenCVCameraConfig

    config = OpenCVCameraConfig(index_or_path=target, fps=fps, width=width, height=height, fourcc=fourcc)
    camera = OpenCVCamera(config)
    try:
        camera.connect()
        frame = camera.read()
    except Exception as exc:  # noqa: BLE001
        return f"camera {name} ({device}): could not capture {width}x{height} at {fps} fps ({first_line(exc)})"
    finally:
        if camera.is_connected:
            camera.disconnect()
    if snapshots is not None:
        snapshots.put(name, frame)
    return f"camera {name} ({device}): captured a {frame.shape[1]}x{frame.shape[0]} frame (picture below)"


# ── calibration mode: record the joint ranges by hand, never write ───────────


def offset_for_home(present, servo_offset):
    """The Homing_Offset that makes the pose now at `present` read HALF_TURN (2047).

    Feetech servos report Present_Position = (actual - Homing_Offset) mod 4096,
    and the register holds -2047..2047 (sign and magnitude). LeRobot's
    set_half_turn_homings computes the same value and writes it; this only
    computes it.
    """
    offset = (present + servo_offset - HALF_TURN + 2048) % TICKS_PER_TURN - 2048
    return max(-2047, offset)  # -2048 does not fit the register; one tick off is harmless


def in_frame(raw, servo_offset, new_offset):
    """Where a Present_Position reading taken with `servo_offset` lands once `new_offset` is in the servo."""
    return (raw + servo_offset - new_offset) % TICKS_PER_TURN


class RangeCapture:
    """Track each joint's lowest and highest reading while the arm is moved by hand.

    Feetech servos occasionally answer a read with garbage. A reading more than
    `max_jump` ticks from the last accepted one is held back until `confirm`
    readings in a row agree with it; then it is real (the poll stalled, or the
    joint crossed the encoder's 0/4095 boundary, which marks it `wrapped`).
    """

    def __init__(self, max_jump=500, confirm=5):
        self.max_jump = max_jump
        self.confirm = confirm
        self.reset()

    def reset(self):
        self.low, self.high, self.anchor = {}, {}, {}
        self.pending = {}  # joint -> readings held back, each within max_jump of the one before
        self.wrapped = set()

    def update(self, values):
        for joint, value in values.items():
            anchor = self.anchor.get(joint)
            if anchor is not None and abs(value - anchor) <= self.max_jump:
                self.pending.pop(joint, None)
                self._accept(joint, [value])
                continue
            pending = self.pending.get(joint)
            if pending and abs(value - pending[-1]) <= self.max_jump:
                pending.append(value)
            else:
                pending = self.pending[joint] = [value]
            if len(pending) >= self.confirm:
                if anchor is not None and abs(pending[0] - anchor) > TICKS_PER_TURN // 2:
                    self.wrapped.add(joint)
                del self.pending[joint]
                self._accept(joint, pending)

    def _accept(self, joint, readings):
        self.anchor[joint] = readings[-1]
        self.low[joint] = min(self.low.get(joint, readings[0]), *readings)
        self.high[joint] = max(self.high.get(joint, readings[0]), *readings)


def min_span_ticks(joint):
    return 200 if joint == GRIPPER else 500


def joint_status(joint, low, high, wrapped, role):
    """'not swept', 'wrapped', 'too wide', 'narrow' or 'ok' for one joint's captured range."""
    if low is None or high is None or low == high:
        return "not swept"
    if wrapped:
        return "wrapped"
    if role == "follower":
        lower, upper = URDF_LIMITS_DEG[joint]
        if (high - low) / DEGREE_TICKS > upper - lower + SPAN_TOLERANCE_DEG:
            return "too wide"
    if high - low < min_span_ticks(joint):
        return "narrow"
    return "ok"


class CalibrationSession:
    """ELROBOT_CALIBRATE: a browser page to calibrate one arm. It never writes to the servos.

    By default the servos' homing offsets are kept (the team's follower holds
    offsets written by norma-core) and only the joint ranges are recorded, in
    the positions the servos report. "Set home here" instead computes new
    homing offsets that make the current pose read 2047 (LeRobot's convention);
    they go into the file only, and reach the servos only when the record or
    policy app runs with ELROBOT_WRITE_CALIBRATION=true.
    """

    def __init__(self, role, port, path, open_bus=open_bus, retry_seconds=5.0, poll_seconds=0.02):
        self.role, self.port, self.path = role, port, Path(path)
        self.open_bus = open_bus
        self.retry_seconds, self.poll_seconds = retry_seconds, poll_seconds
        self.lock = threading.Lock()  # state below
        self.bus_lock = threading.Lock()  # one servo transaction at a time
        self.bus = None
        self.state, self.detail, self.error = "connecting", f"opening {port}", None
        self.servo = {}  # register -> {joint: value}, read once at connect
        self.new_offsets = None  # joint -> offset after "Set home here", or None to keep the servos'
        self.capture = RangeCapture()
        self.capturing = False
        self.live = {}
        self.saved = None

    # ── page actions ────────────────────────────────────────────────────────

    def _require_bus(self):
        if self.bus is None:
            raise SettingsError(f"the {self.role} arm is not connected yet: {self.detail}")

    def offsets(self):
        """The homing offsets that go into the file: new ones after Set home, else the servos'."""
        if self.new_offsets:
            return dict(self.new_offsets)
        return dict(self.servo.get("Homing_Offset", {}))

    def frame(self, raw):
        """Readings as they will read with the offsets that go into the file."""
        if not self.new_offsets:
            return dict(raw)
        servo = self.servo["Homing_Offset"]
        return {joint: in_frame(raw[joint], servo[joint], self.new_offsets[joint]) for joint in raw}

    def set_home(self):
        self._require_bus()
        present = self._read_present()
        with self.lock:
            servo = self.servo["Homing_Offset"]
            self.new_offsets = {joint: offset_for_home(present[joint], servo[joint]) for joint in JOINTS}
            self.capture.reset()
            self.saved = None
        return "home set (computed only, nothing written); now sweep every joint"

    def keep_home(self):
        self._require_bus()
        with self.lock:
            self.new_offsets = None
            self.capture.reset()
            self.saved = None
        return "keeping the servos' homing offsets; sweep every joint"

    def start(self):
        self._require_bus()
        with self.lock:
            self.capture.reset()
            self.capturing = True
            self.saved = None
        return "recording ranges: move every joint from end to end"

    def stop(self):
        with self.lock:
            self.capturing = False
        return "stopped recording ranges"

    def calibration(self):
        """The LeRobot calibration dict and per-joint status from the capture so far."""
        offsets = self.offsets()
        with self.lock:
            low, high, wrapped = dict(self.capture.low), dict(self.capture.high), set(self.capture.wrapped)
        data, status = {}, {}
        for joint in JOINTS:
            status[joint] = joint_status(joint, low.get(joint), high.get(joint), joint in wrapped, self.role)
            if joint in low and joint in offsets:
                data[joint] = {
                    "id": SERVO_IDS[joint],
                    "drive_mode": 0,
                    "homing_offset": int(offsets[joint]),
                    "range_min": int(low[joint]),
                    "range_max": int(high[joint]),
                }
        return data, status

    def save(self):
        self._require_bus()
        with self.lock:
            if self.capturing:
                raise SettingsError("stop recording ranges first")
        data, status = self.calibration()
        bad = {joint: state for joint, state in status.items() if state in ("not swept", "wrapped", "too wide")}
        if bad:
            hints = []
            if any(state == "wrapped" or state == "too wide" for state in bad.values()):
                hints.append(
                    "a joint crossed the encoder's 0/4095 boundary: put the arm in the middle of its travel, "
                    "press Set home here, and record again"
                )
            raise SettingsError(
                "cannot save: " + ", ".join(f"{joint} {state}" for joint, state in bad.items())
                + (". " + "; ".join(hints) if hints else "")
            )
        warnings = check_calibration(data, str(self.path), self.role)
        warnings += [f"{joint}: only {data[joint]['range_max'] - data[joint]['range_min']} ticks swept"
                     for joint, state in status.items() if state == "narrow"]
        # The file's homing offsets must match the servos as they are now (no one else wrote them).
        current = self._read("Homing_Offset")
        if current != self.servo["Homing_Offset"]:
            raise SettingsError("the servos' homing offsets changed since this page connected; restart the app")
        backup = write_calibration_file(self.path, data)
        with self.lock:
            self.saved = {"path": str(self.path), "backup": backup, "warnings": warnings,
                          "writes_needed": bool(self.new_offsets)}
        log.info("saved %s calibration to %s%s", self.role, self.path, f" (previous file kept as {backup})" if backup else "")
        return f"saved {self.path}" + (f"; warnings: {'; '.join(warnings)}" if warnings else "")

    # ── servo access (reads only) ───────────────────────────────────────────

    def _read(self, register):
        with self.bus_lock:
            if self.bus is None:
                raise SettingsError(f"the {self.role} arm is not connected")
            return self.bus.sync_read(register, normalize=False, num_retry=2)

    def _read_present(self):
        return self._read("Present_Position")

    def connect_once(self):
        try:
            bus = self.open_bus(self.port)
        except Exception as exc:  # noqa: BLE001 - shown on the page, retried
            hint = " Is its servo supply switched on?" if "Missing motor IDs" in str(exc) else ""
            with self.lock:
                self.detail = f"{self.port}: servos did not answer ({first_line(exc)}).{hint} Retrying."
            return False
        self.bus = bus
        try:
            servo = {name: self._read(name) for name in PROBE_REGISTERS}
        except Exception as exc:  # noqa: BLE001 - a bad read: close the port and retry
            self.close()
            with self.lock:
                self.detail = f"{self.port}: servos answered, but a read failed ({first_line(exc)}). Retrying."
            return False
        with self.lock:
            self.servo = servo
            self.state, self.detail = "ready", f"{self.role} on {self.port}: torque is " + (
                "ON for " + ", ".join(j for j in JOINTS if servo["Torque_Enable"][j])
                + "; the arm is held and cannot be moved by hand"
                if any(servo["Torque_Enable"].values()) else "off; move the arm by hand"
            )
        return True

    def run(self, stopping):
        """Connect (retrying), then poll positions until `stopping` (a threading.Event) is set."""
        try:
            while not stopping.is_set() and not self.connect_once():
                stopping.wait(self.retry_seconds)
            while not stopping.is_set():
                try:
                    raw = self._read_present()
                except Exception as exc:  # noqa: BLE001 - one failed read is not fatal
                    log.debug("position read failed: %s", first_line(exc))
                    stopping.wait(self.poll_seconds)
                    continue
                values = self.frame(raw)
                with self.lock:
                    self.live = values
                    if self.capturing:
                        self.capture.update(values)
                stopping.wait(self.poll_seconds)
        except Exception as exc:  # noqa: BLE001 - keep serving the page with the error
            log.exception("calibration failed")
            with self.lock:
                self.state, self.error = "error", f"{type(exc).__name__}: {exc}"
        finally:
            self.close()

    def close(self):
        bus, self.bus = self.bus, None
        if bus is not None:
            try:
                with self.bus_lock:
                    bus.disconnect(disable_torque=False)  # closing the port must not write to the servos
            except Exception as exc:  # noqa: BLE001
                log.warning("could not close %s: %s", self.port, first_line(exc))

    def status(self):
        data, status = self.calibration()
        with self.lock:
            joints = []
            for joint in JOINTS:
                entry = data.get(joint, {})
                low, high = entry.get("range_min"), entry.get("range_max")
                joints.append({
                    "joint": joint,
                    "id": SERVO_IDS[joint],
                    "position": self.live.get(joint),
                    "min": low,
                    "max": high,
                    "span_deg": None if low is None else round((high - low) / DEGREE_TICKS, 1),
                    "urdf_deg": round(URDF_LIMITS_DEG[joint][1] - URDF_LIMITS_DEG[joint][0], 1)
                    if self.role == "follower" else None,
                    "status": status[joint],
                })
            return {
                "mode": "calibrate",
                "role": self.role,
                "port": self.port,
                "path": str(self.path),
                "state": self.state,
                "detail": self.detail,
                "error": self.error,
                "home": "new home (written to the servos only with ELROBOT_WRITE_CALIBRATION=true)"
                if self.new_offsets else "the servos' current homing offsets (nothing to write)",
                "capturing": self.capturing,
                "joints": joints,
                "saved": self.saved,
            }

    def actions(self):
        return {"home": self.set_home, "keep": self.keep_home, "start": self.start, "stop": self.stop,
                "save": self.save}


def write_calibration_file(path, data):
    """Write `data` as JSON atomically; keep a previous file as <name>.<time>.bak. Returns the backup path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}.bak")
        shutil.copy2(path, backup)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as out:
            json.dump(data, out, indent=4)
            out.write("\n")
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return str(backup) if backup else None


CALIBRATION_PAGE = """<!doctype html><html><head><meta charset=utf-8><title>ElRobot calibration</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{font-family:ui-monospace,Menlo,monospace;background:#0c0c0c;color:#e8e8e8;margin:20px;max-width:980px}
button{font-size:15px;padding:9px 14px;margin:5px 5px 5px 0}.muted{color:#888}.err{color:#f66;white-space:pre-wrap}
table{border-collapse:collapse;margin:10px 0}td,th{border:1px solid #444;padding:4px 10px;text-align:right}
td.j{text-align:left}.ok{color:#7f7}.narrow{color:#fc5}.bad{color:#f66}#msg{color:#fc5;white-space:pre-wrap}</style>
</head><body>
<h2>ElRobot calibration: <span id=role></span></h2>
<div class=muted>This page only reads from the servos. The arm should be limp (torque off) so you can move it by hand.</div>
<div id=state></div><div id=home class=muted></div>
<ol class=muted>
<li>Keep the servos' homing offsets (the default; right for an arm calibrated by norma-core), or put the arm in the
middle of every joint's travel and press <b>Set home here</b>.</li>
<li><b>Start</b>, move every joint slowly from one end of its travel to the other, then <b>Stop</b>.</li>
<li><b>Save</b> writes the LeRobot calibration file. Then run the app again without ELROBOT_CALIBRATE.</li></ol>
<button onclick="send('keep')">Keep the servos' homing</button>
<button onclick="send('home')">Set home here</button>
<button onclick="send('start')">Start</button>
<button onclick="send('stop')">Stop</button>
<button onclick="send('save')">Save</button>
<div id=msg></div><div id=err class=err></div>
<table id=tbl></table>
<script>
const CLS = {'ok': 'ok', 'narrow': 'narrow', 'not swept': 'bad', 'wrapped': 'bad', 'too wide': 'bad'};
async function send(action){
  const r = await fetch('/api/' + action, {method: 'POST'});
  const body = await r.json();
  document.getElementById('msg').textContent = r.ok ? body.result : '';
  document.getElementById('err').textContent = r.ok ? '' : body.error;
  poll();
}
async function poll(){
  let d; try { d = await (await fetch('/api/status')).json(); } catch (e) { return; }
  document.getElementById('role').textContent = d.role + ' on ' + d.port + ' -> ' + d.path;
  document.getElementById('state').textContent = d.state + ': ' + d.detail + (d.capturing ? ' (recording ranges)' : '');
  document.getElementById('home').textContent = 'homing offsets: ' + d.home;
  if (d.error) document.getElementById('err').textContent = d.error;
  let h = '<tr><th class=j>joint</th><th>id</th><th>position</th><th>min</th><th>max</th><th>swept deg</th>' +
    (d.role === 'follower' ? '<th>URDF deg</th>' : '') + '<th>status</th></tr>';
  for (const j of d.joints) {
    h += '<tr><td class=j>' + j.joint + '</td><td>' + j.id + '</td><td>' + (j.position ?? '-') + '</td><td>' +
      (j.min ?? '-') + '</td><td>' + (j.max ?? '-') + '</td><td>' + (j.span_deg ?? '-') + '</td>' +
      (d.role === 'follower' ? '<td>' + j.urdf_deg + '</td>' : '') +
      '<td class=' + (CLS[j.status] || 'bad') + '>' + j.status + '</td></tr>';
  }
  document.getElementById('tbl').innerHTML = h;
  if (d.saved) document.getElementById('msg').textContent = 'saved ' + d.saved.path +
    (d.saved.backup ? ' (previous file kept as ' + d.saved.backup + ')' : '') +
    (d.saved.warnings.length ? '\\nwarnings: ' + d.saved.warnings.join('; ') : '') +
    (d.saved.writes_needed ? '\\nThe new home is in the file only: run with ELROBOT_WRITE_CALIBRATION=true once to write it to the servos.' : '');
}
setInterval(poll, 300); poll();
</script></body></html>"""


def run_calibration(role, port, path, http_port, stopping):
    """Serve the calibration page for one arm until `stopping` is set."""
    session = CalibrationSession(role, port, path)
    ControlServer(http_port, CALIBRATION_PAGE, session.status, session.actions()).start()
    log.info("calibration page for the %s (%s -> %s) on http://0.0.0.0:%d", role, port, path, http_port)
    session.run(stopping)


def first_line(exc):
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__
