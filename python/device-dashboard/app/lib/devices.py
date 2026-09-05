import glob
import subprocess
from pathlib import Path


def v4l2_device_name(path: str) -> str:
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", "--device", path, "--info"],
            stderr=subprocess.DEVNULL, timeout=2,
        ).decode()
        for line in out.splitlines():
            if "Card type" in line:
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return Path(path).name


def v4l2_is_capture(path: str) -> bool:
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", "--device", path, "--all"],
            stderr=subprocess.DEVNULL, timeout=2,
        ).decode()
        return "Video Capture" in out
    except Exception:
        return False


def list_cameras() -> list[dict]:
    cameras = []
    for path in sorted(glob.glob("/dev/video*")):
        if v4l2_is_capture(path):
            cameras.append({"id": path, "name": v4l2_device_name(path)})
    return cameras


def list_alsa_devices(cmd: str) -> list[dict]:
    devs = []
    try:
        out = subprocess.check_output(cmd.split(), stderr=subprocess.DEVNULL, timeout=2).decode()
        for line in out.splitlines():
            if line.startswith("card "):
                parts = line.split(":")
                if len(parts) >= 2:
                    card = line.split()[1].rstrip(":")
                    name = parts[1].strip().split("[")[0].strip()
                    devs.append({"id": f"hw:{card},0", "name": name})
    except Exception:
        pass
    return devs
