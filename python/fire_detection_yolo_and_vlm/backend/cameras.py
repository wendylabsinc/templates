"""Camera discovery and MJPEG streaming via GStreamer.

Cross-platform:
- macOS: avfvideosrc with device-index (resolved from name via gst-device-monitor)
- Linux: v4l2src with /dev/videoN
- Jetson/DeepStream: v4l2src with nvvidconv + nvjpegenc for HW acceleration

On Linux the discovery prefers stable USB-camera identifiers under
/dev/v4l/by-id, then falls back to capture-classified /dev/video* nodes
sorted by sysfs index. Multi-stream USB cameras (RealSense, Orbbec) often
register several /dev/videoN where only one is a Video Capture node;
v4l2-ctl --all is consulted to filter the others out.
"""

import glob
import platform
import re
import subprocess
import threading
import time
from pathlib import Path

_cache: list[dict] | None = None
_cache_time: float = 0
_CACHE_TTL = 30.0
_lock = threading.Lock()

IS_DARWIN = platform.system() == "Darwin"

# Map camera name → device-index, refreshed with each discovery.
_name_to_index: dict[str, int] = {}


# -- Linux V4L2 helpers (sysfs + v4l2-ctl) --

_V4L_SYMLINK_DIRS = (Path("/dev/v4l/by-id"), Path("/dev/v4l/by-path"))


def _sysfs_video_node(path: str) -> Path:
    return Path("/sys/class/video4linux") / Path(path).name


def _v4l2_node_index(path: str) -> int:
    """Sysfs-reported index for a /dev/videoN node — stable across reboots
    for a given physical camera/sub-stream pairing. Returns 999 on failure
    so unknown nodes sort last."""
    try:
        return int((_sysfs_video_node(path) / "index").read_text().strip())
    except Exception:
        return 999


def _v4l2_card_name(path: str) -> str:
    """Card type from `v4l2-ctl --info`, or basename if v4l2-ctl is missing."""
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", "--device", path, "--info"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode()
        for line in out.splitlines():
            if "Card type" in line:
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return Path(path).name


def _v4l2_is_capture(path: str) -> bool:
    """True when this node advertises Video Capture in its Device Caps.
    Filters out metadata/output-only sub-devices (e.g. RealSense IMU/IR
    streams that share /dev/videoN nodes with the RGB capture).

    If v4l2-ctl is unavailable we return True (permissive) so cv2 still
    gets a chance — better than dropping every node."""
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", "--device", path, "--all"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode()
    except FileNotFoundError:
        return True
    except Exception:
        return False
    in_caps = False
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("Device Caps"):
            in_caps = True
            continue
        if not in_caps:
            continue
        if not line.startswith((" ", "\t")):
            break
        if stripped in {"Video Capture", "Video Capture Multiplanar"}:
            return True
    return False


def _usb_device_id(path: str) -> str | None:
    """USB topology id for a video node (e.g. '1-2.3'), or None for
    non-USB devices (CSI, virtual, etc.). Used to prefer USB-backed
    nodes when symlinks aren't available."""
    try:
        device_path = (_sysfs_video_node(path) / "device").resolve()
    except Exception:
        return None
    for current in [device_path] + list(device_path.parents):
        if (current / "idVendor").exists() and (current / "idProduct").exists():
            return current.name.split(":", 1)[0]
    return None


def _linux_symlink_video_nodes() -> list[str]:
    """USB cameras exposed via stable symlinks. /dev/v4l/by-id survives
    plug-order changes and is the right thing to target when present."""
    nodes: list[str] = []

    def add(p: str) -> None:
        if p not in nodes:
            nodes.append(p)

    by_id = _V4L_SYMLINK_DIRS[0]
    if by_id.is_dir():
        for link in sorted(by_id.iterdir()):
            if not link.name.startswith("usb-"):
                continue
            try:
                target = link.resolve()
            except Exception:
                continue
            if target.name.startswith("video"):
                add(f"/dev/{target.name}")
    if nodes:
        return nodes

    by_path = _V4L_SYMLINK_DIRS[1]
    if by_path.is_dir():
        for link in sorted(by_path.iterdir()):
            if "-usb-" not in link.name and "-usbv" not in link.name:
                continue
            try:
                target = link.resolve()
            except Exception:
                continue
            if target.name.startswith("video"):
                add(f"/dev/{target.name}")
    return nodes


def _linux_candidate_video_nodes() -> list[str]:
    """Ordered list of plausible camera nodes:
      1. USB symlinks under /dev/v4l/by-id (most stable)
      2. /dev/video* sorted by sysfs index, USB-backed only (when any are USB)
      3. /dev/video* sorted by sysfs index, all of them (fallback)
    """
    sym = _linux_symlink_video_nodes()
    if sym:
        return sym
    nodes = sorted(glob.glob("/dev/video*"), key=lambda p: (_v4l2_node_index(p), p))
    if not nodes:
        return nodes
    usb = [p for p in nodes if _usb_device_id(p)]
    return usb if usb else nodes


def discover_capture_nodes() -> list[dict]:
    """Capture-capable Linux V4L2 nodes, ready to feed into a v4l2src
    pipeline or cv2.VideoCapture. Empty list on macOS / no cameras.

    Each entry: {"path", "v4l_index", "usb", "name"}."""
    if IS_DARWIN:
        return []
    out: list[dict] = []
    for path in _linux_candidate_video_nodes():
        if not _v4l2_is_capture(path):
            continue
        out.append({
            "path": path,
            "v4l_index": _v4l2_node_index(path),
            "usb": _usb_device_id(path),
            "name": _v4l2_card_name(path),
        })
    return out


# -- Discovery (public, cached, returns shape used by /cameras endpoint) --


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
    """Discover capture-capable V4L2 devices on Linux.

    Output shape preserved for backward compatibility with consumers of
    the /cameras HTTP endpoint and mjpeg_frames(camera_id): `id` is the
    string-form numeric basename ("0" for /dev/video0). Use
    discover_capture_nodes() instead when a richer record is needed.
    """
    cameras = []
    for node in discover_capture_nodes():
        path = node["path"]
        idx = Path(path).name.replace("video", "")
        cameras.append({"id": idx, "name": node["name"], "available": True})
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
