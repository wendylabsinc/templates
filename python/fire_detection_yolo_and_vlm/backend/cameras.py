"""Camera discovery and MJPEG streaming via GStreamer.

Cross-platform:
- macOS: avfvideosrc with device-index (resolved from name via gst-device-monitor)
- Linux: v4l2src with /dev/videoN
- Jetson/DeepStream: v4l2src with nvvidconv + nvjpegenc for HW acceleration
"""

import platform
import re
import subprocess
import threading
import time

_cache: list[dict] | None = None
_cache_time: float = 0
_CACHE_TTL = 30.0
_lock = threading.Lock()

IS_DARWIN = platform.system() == "Darwin"

# Map camera name → device-index, refreshed with each discovery.
_name_to_index: dict[str, int] = {}


# -- Discovery --


def _discover_macos() -> list[dict]:
    """Discover cameras via gst-device-monitor-1.0 on macOS.

    Parses both the device name and the device-index from the output,
    which gives us the authoritative name→index mapping.
    """
    global _name_to_index

    try:
        result = subprocess.run(
            ["gst-device-monitor-1.0", "Video/Source"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []

    cameras: list[dict] = []
    name_to_idx: dict[str, int] = {}
    current_name: str | None = None

    for line in result.stdout.splitlines():
        stripped = line.strip()

        # "name  : Elgato Facecam Pro"
        m = re.match(r"name\s*:\s*(.+)", stripped)
        if m:
            current_name = m.group(1).strip()
            continue

        # "gst-launch-1.0 avfvideosrc device-index=0 ! ..."
        if current_name and "device-index=" in stripped:
            m = re.search(r"device-index=(\d+)", stripped)
            if m:
                idx = int(m.group(1))
                if not current_name.lower().startswith("capture screen"):
                    name_to_idx[current_name] = idx
                    cameras.append({"id": current_name, "name": current_name, "available": True})
                current_name = None

    _name_to_index = name_to_idx
    return cameras


def _discover_linux() -> list[dict]:
    """Discover V4L2 devices on Linux."""
    import glob
    import os

    cameras = []
    for path in sorted(glob.glob("/dev/video*")):
        idx = os.path.basename(path).replace("video", "")
        try:
            result = subprocess.run(
                ["v4l2-ctl", "-d", path, "--info"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            name = f"Camera {idx}"
            for line in result.stdout.splitlines():
                if "Card type" in line:
                    name = line.split(":", 1)[1].strip()
                    break
            cameras.append({"id": idx, "name": name, "available": True})
        except Exception:
            cameras.append({"id": idx, "name": f"Camera {idx}", "available": True})
    return cameras


def discover_cameras() -> list[dict]:
    """Return available cameras. Results are cached."""
    global _cache, _cache_time

    with _lock:
        now = time.monotonic()
        if _cache is not None and (now - _cache_time) < _CACHE_TTL:
            return _cache

    cameras = _discover_macos() if IS_DARWIN else _discover_linux()

    with _lock:
        _cache = cameras
        _cache_time = time.monotonic()

    return cameras


# -- GStreamer MJPEG Streaming --


_gst_element_cache: dict[str, bool] = {}


def _has_gst_element(name: str) -> bool:
    if name not in _gst_element_cache:
        try:
            r = subprocess.run(["gst-inspect-1.0", name], capture_output=True, timeout=3)
            _gst_element_cache[name] = r.returncode == 0
        except Exception:
            _gst_element_cache[name] = False
    return _gst_element_cache[name]


def _gst_pipeline(camera_id: str) -> str:
    """Build a gst-launch shell command for MJPEG streaming."""
    if IS_DARWIN:
        # Resolve name → device-index. Re-discover if not in cache.
        idx = _name_to_index.get(camera_id)
        if idx is None:
            _discover_macos()
            idx = _name_to_index.get(camera_id, 0)
        return (
            f"gst-launch-1.0 -q "
            f"avfvideosrc device-index={idx} ! "
            f"video/x-raw,framerate=30/1 ! "
            f"videoconvert ! "
            f"jpegenc quality=70 ! "
            f"multipartmux boundary=frame ! "
            f"fdsink"
        )
    else:
        if _has_gst_element("nvjpegenc"):
            return (
                f"gst-launch-1.0 -q "
                f"v4l2src device=/dev/video{camera_id} ! "
                f"video/x-raw,framerate=30/1 ! "
                f"nvvidconv ! "
                f"nvjpegenc ! "
                f"multipartmux boundary=frame ! "
                f"fdsink"
            )
        return (
            f"gst-launch-1.0 -q "
            f"v4l2src device=/dev/video{camera_id} ! "
            f"video/x-raw,framerate=30/1 ! "
            f"videoconvert ! "
            f"jpegenc quality=70 ! "
            f"multipartmux boundary=frame ! "
            f"fdsink"
        )


# Track active stream processes so we can kill them on camera switch.
_active_procs: dict[str, subprocess.Popen] = {}
_proc_lock = threading.Lock()


def _kill_active_stream(camera_id: str):
    """Kill any existing stream for this camera."""
    with _proc_lock:
        proc = _active_procs.pop(camera_id, None)
    if proc:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


def mjpeg_frames(camera_id: str):
    """Stream MJPEG frames from a camera using GStreamer."""
    # Kill any existing stream for this camera first
    _kill_active_stream(camera_id)

    cmd = _gst_pipeline(camera_id)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        shell=True,
    )

    with _proc_lock:
        _active_procs[camera_id] = proc

    try:
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            yield chunk
    finally:
        with _proc_lock:
            _active_procs.pop(camera_id, None)
        proc.kill()
        proc.wait()
