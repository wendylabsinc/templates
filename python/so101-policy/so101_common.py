"""Helpers shared by the so101-record and so101-policy templates.

Both templates carry a byte-identical copy of this file, because `wendy init`
copies one template directory on its own; a repository test keeps the copies
equal. Module level uses the standard library only, so the settings code can be
unit-tested without LeRobot. The functions that touch hardware import LeRobot
inside their bodies.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# The SO-101 joints in LeRobot's motor order (servo IDs 1 to 6). Datasets and
# policies use this order, so it is fixed rather than configurable.
JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")
SERVO_IDS = {joint: index + 1 for index, joint in enumerate(JOINTS)}
CALIBRATION_FIELDS = ("id", "drive_mode", "homing_offset", "range_min", "range_max")
MAX_TICK = 4095  # STS3215 encoder: 0 to 4095 ticks per turn

# The recorder writes this task sentence into the dataset unless SO101_TASK is
# set, and the policy runner sends the same sentence to language-conditioned
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
    """A serial device path such as /dev/ttyACM0."""
    port = env_str(env, name, default)
    if not port.startswith("/dev/"):
        raise SettingsError(f"{name}={port!r} must be a device path such as /dev/ttyACM0")
    return port


def camera_fourcc(env, name="SO101_CAMERA_FOURCC", default="MJPG"):
    """A 4-character pixel format, or None for the camera's default (set the variable to `auto`)."""
    value = env_str(env, name, default)
    if value.lower() in ("", "auto"):
        return None
    if len(value) != 4:
        raise SettingsError(f"{name}={value!r} must be 4 characters, such as MJPG or YUYV, or auto")
    return value


def parse_cameras(text, default_name=None, variable="SO101_CAMERAS"):
    """Parse comma-separated `name=/dev/videoN` entries into {name: device}.

    A bare device without `name=` gets `default_name`. When that is None the
    name stays None, and the policy runner fills in the model's camera name.
    """
    cameras = {}
    for item in (part.strip() for part in text.split(",")):
        if not item:
            continue
        if "=" in item:
            name, device = (piece.strip() for piece in item.split("=", 1))
            if not CAMERA_NAME.match(name):
                raise SettingsError(
                    f"{variable}: camera name {name!r} must be lowercase letters, digits and _, "
                    "starting with a letter (it becomes observation.images.<name>)"
                )
        else:
            name, device = default_name, item
        if not device:
            raise SettingsError(f"{variable}: {item!r} has no device")
        if not (device.isdigit() or device.startswith("/dev/")):
            raise SettingsError(f"{variable}: {device!r} must be a device path such as /dev/video0")
        if name in cameras:
            label = name if name is not None else "(unnamed)"
            raise SettingsError(f"{variable}: camera {label} is listed twice")
        cameras[name] = device
    if not cameras:
        raise SettingsError(f"{variable} is empty; set it to name=/dev/videoN, for example front=/dev/video0")
    return cameras


def camera_index_or_path(device):
    """LeRobot's OpenCV camera takes an integer index or a device path."""
    return int(device) if device.isdigit() else Path(device)


# ── calibration files ────────────────────────────────────────────────────────


@dataclass
class CalibrationFile:
    path: Path
    data: dict
    warnings: list = field(default_factory=list)


def load_calibration(path, role, variable):
    """Read and check a calibration JSON written by so101-calibration or LeRobot.

    `role` is "follower" or "leader" (for messages); `variable` is the env var
    that names the file. Raises SettingsError when the file cannot be used.
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
            f"No {role} calibration at {path}.{hint} Calibrate the {role} arm with the so101-calibration "
            "template, check that CALIBRATION_VOLUME in wendy.json names its volume, "
            f"or set {variable} to the file."
        )
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SettingsError(f"{path} is not readable JSON: {exc}") from None
    return CalibrationFile(path, data, check_calibration(data, str(path)))


def check_calibration(data, label):
    """Validate LeRobot's per-motor calibration shape; return a list of warnings."""
    if not isinstance(data, dict):
        raise SettingsError(f"{label}: expected a JSON object with one entry per joint")
    missing = [joint for joint in JOINTS if joint not in data]
    if missing:
        raise SettingsError(f"{label}: no entry for {', '.join(missing)}")
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
        if values["range_min"] >= values["range_max"]:
            raise SettingsError(
                f"{label}: {joint} range_min {values['range_min']} is not below range_max {values['range_max']}; "
                "sweep the joint again and save"
            )
        # LeRobot writes range_min/max into the servo's position-limit registers,
        # which hold 0 to 4095. A homed range outside that usually means the arm
        # was not in its middle pose when home was set.
        if values["range_min"] < 0 or values["range_max"] > MAX_TICK:
            warnings.append(
                f"{joint}: range {values['range_min']}..{values['range_max']} is outside 0..{MAX_TICK}; "
                "set home with the joint in the middle of its travel and calibrate again"
            )
    return warnings


# ── LeRobot device classes without terminal prompts ──────────────────────────


def without_prompts(cls, hold_on_connect):
    """Subclass a LeRobot SO follower or leader so it never waits for a terminal.

    LeRobot's connect() calls calibrate() when the servos do not hold the
    calibration file's values, and calibrate() asks on stdin whether to write
    the file or to calibrate interactively. A Wendy app has no terminal, so the
    subclass writes the file, with torque off so no joint jumps when its homing
    offset changes. Without a file there is nothing to write, and connect()
    fails with a message instead of waiting for input.

    With `hold_on_connect` (the follower), configure() first sets each servo's
    goal to its present position, so the arm holds still when LeRobot turns
    torque on. The leader never gets torque, so it does not need this.
    """

    class NoPrompt(cls):
        def calibrate(self):
            if not self.calibration:
                raise SettingsError(f"no calibration file at {self.calibration_fpath}")
            log.info("writing %s to the %s servos", self.calibration_fpath, self.id)
            self.bus.disable_torque()
            self.bus.write_calibration(self.calibration)

        def configure(self):
            if hold_on_connect:
                hold_present_position(self.bus)
            super().configure()

    NoPrompt.__name__ = NoPrompt.__qualname__ = f"{cls.__name__}NoPrompt"
    return NoPrompt


def hold_present_position(bus):
    """Make each servo's goal its present position, so turning torque on moves nothing."""
    present = bus.sync_read("Present_Position", num_retry=2)
    bus.sync_write("Goal_Position", present, num_retry=2)


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


def move_smoothly(robot, target, seconds, fps=30, sleep=time.sleep, should_stop=None):
    """Move the follower from where it is to `target` (`<joint>.pos` values) over `seconds`.

    Writes linearly interpolated goal positions at `fps`, like the return to
    the initial position in lerobot-rollout, through the same servo bus write
    LeRobot's send_action() makes. Returns the number of goals sent.
    `should_stop` (a callable) ends the move early.
    """
    if seconds <= 0 or not target:
        return 0
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
    """A small HTTP server: GET / (page), GET /api/status, POST /api/<action>.

    POSTs from another website are refused, so a page on the internet cannot
    drive the arm through the user's browser.
    """

    def __init__(self, port, page, status, actions):
        self.port = port
        self.page = page.encode()
        self.status = status
        self.actions = actions
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


# ── read-only hardware probes (dry run) ──────────────────────────────────────


def probe_arm(port, calibration):
    """Ping the servos on `port` and read their positions. Never writes to the bus.

    Returns a short report line. `calibration` is a CalibrationFile or None.
    """
    if not os.path.exists(port):
        return f"{port}: not present in the container"
    from lerobot.motors import Motor, MotorCalibration, MotorNormMode
    from lerobot.motors.feetech import FeetechMotorsBus

    motors = {joint: Motor(SERVO_IDS[joint], "sts3215", MotorNormMode.RANGE_M100_100) for joint in JOINTS}
    cal = None
    if calibration is not None:
        cal = {
            joint: MotorCalibration(**{name: calibration.data[joint][name] for name in CALIBRATION_FIELDS})
            for joint in JOINTS
        }
    bus = FeetechMotorsBus(port=port, motors=motors, calibration=cal)
    try:
        # The handshake pings each servo and reads its model and firmware: reads only.
        bus.connect(handshake=True)
    except Exception as exc:  # noqa: BLE001 - report any failure as text
        return f"{port}: servos did not answer ({first_line(exc)})"
    try:
        raw = bus.sync_read("Present_Position", normalize=False)
        torque = {joint: bus.read("Torque_Enable", joint, normalize=False) for joint in JOINTS}
        held = bus.is_calibrated if cal is not None else None  # compares registers with the file: reads only
    except Exception as exc:  # noqa: BLE001
        return f"{port}: servos answered, but a read failed ({first_line(exc)})"
    finally:
        # disable_torque=False: closing the port must not write to the servos.
        bus.disconnect(disable_torque=False)
    positions = ", ".join(f"{joint}={raw[joint]}" for joint in JOINTS)
    torque_on = [joint for joint in JOINTS if torque[joint]]
    torque_text = f"torque ON for {', '.join(torque_on)}" if torque_on else "torque off"
    held_text = ""
    if held is not None:
        held_text = "; servos hold the calibration file" if held else "; servos do not hold the calibration file yet"
    return f"{port}: 6/6 servos answered, {torque_text}{held_text}; raw ticks {positions}"


def probe_camera(name, device, width, height, fps, fourcc):
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
    return f"camera {name} ({device}): captured a {frame.shape[1]}x{frame.shape[0]} frame"


def first_line(exc):
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__
