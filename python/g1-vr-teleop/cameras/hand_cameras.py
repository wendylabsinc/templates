"""Hand cameras: the two USB cameras in the G1's hands, served to teleop.

Why a service of its own: the onboard commander is the only code that moves
the arms, and it runs a 100 Hz Python loop that has already once starved its
publish loop of the GIL (onboard/g1_arm.py). Camera capture inside it would
compete for that lock and for its event loop. Here a stalled camera can only
slow itself down.

Shape: one thread per camera grabs MJPG frames and keeps only the newest.
The camera's own JPEG goes out untouched -- no decode, no re-encode -- so two
cameras at 30 fps cost almost no CPU. Each frame is stamped right after
grab() on CLOCK_MONOTONIC, the clock the bridge and the recorder share on
this host, plus wall time for people.

Which device is which: identical cameras share one USB serial, so their
/dev/v4l/by-id links collide, and /dev/videoN numbers move between boots.
Cameras are picked by their /dev/v4l/by-path link -- the USB port -- and the
link is resolved again on every open. Software cannot tell the left hand from
the right, so the console shows both feeds labelled by port with a Swap
button, and the choice is saved in /state/cameras.json.

No camera controls are set (exposure, gain, ...): on another G1, a retry storm
of failing UVC control requests wedged the USB controller that also carries
the robot's Wi-Fi. Reopens back off for the same reason.

Serves, on HAND_CAM_PORT (8091), host networking:
  GET  /status                 every camera: device, port, fps, errors
  GET  /cam/{name}/frame.jpg   newest JPEG; X-Frame-Seq, X-Captured-Mono-Ns,
                               X-Captured-Unix-Ns headers
  GET  /cam/{name}             websocket of packed frames, the same packing as
                               the onboard's /cam (4-byte length, JSON, JPEG)
  POST /swap                   swap left and right, saved across restarts
"""
import asyncio
import json
import os
import re
import struct
import threading
import time
from collections import deque

SYS_V4L = "/sys/class/video4linux"
BY_PATH = "/dev/v4l/by-path"
NAMES = ("left", "right")
OFF = ("0", "off", "no", "false")


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def usb_port(by_path):
    """platform-3610000.xhci-usb-0:2.3.4:1.0-video-index0 -> '2.3.4'."""
    m = re.search(r"usb-\d+:([\d.]+):", os.path.basename(by_path or ""))
    return m.group(1) if m else None


def list_capture_nodes(by_path_dir=BY_PATH, sys_dir=SYS_V4L):
    """-> [{by_path, dev, name}] for every video-index0 link, sorted by port.

    index0 is a UVC camera's capture node; index1 is its metadata node, which
    cannot deliver frames and is skipped.
    """
    try:
        links = sorted(os.listdir(by_path_dir))
    except OSError:
        return []
    out = []
    for link in links:
        if not link.endswith("-video-index0"):
            continue
        full = os.path.join(by_path_dir, link)
        dev = os.path.realpath(full)
        name = _read(os.path.join(sys_dir, os.path.basename(dev), "name"))
        out.append({"by_path": full, "dev": dev, "name": name})
    return out


def auto_candidates(nodes, match=""):
    """The nodes `auto` may use.

    With HAND_CAM_MATCH set, only cameras whose name contains it. Otherwise
    every capture node except a RealSense, which is the head camera's.
    """
    if match:
        return [n for n in nodes if match.lower() in n["name"].lower()]
    return [n for n in nodes if "realsense" not in n["name"].lower()]


def normalize(value, by_path_dir=BY_PATH):
    """'auto', a by-path link name, or an absolute path -> 'auto' or a path."""
    value = (value or "auto").strip()
    if value.lower() == "auto":
        return "auto"
    return value if value.startswith("/") else os.path.join(by_path_dir, value)


def load_mapping(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
        return {k: data[k] for k in NAMES if isinstance(data.get(k), str)}
    except (OSError, ValueError, AttributeError):
        return {}


def save_mapping(path, mapping):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({k: mapping.get(k) for k in NAMES}, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def assign(left, right, nodes, match="", saved=None):
    """-> {'left': path or None, 'right': path or None}.

    An explicit path always wins. `auto` takes, in port order, the candidates
    an explicit choice has not already taken -- unless a saved mapping (from
    the Swap button) names candidates that are still plugged in.
    """
    left, right = normalize(left), normalize(right)
    out = {"left": None if left == "auto" else left,
           "right": None if right == "auto" else right}
    taken = {p for p in out.values() if p}
    free = [n["by_path"] for n in auto_candidates(nodes, match)
            if n["by_path"] not in taken]
    saved = saved or {}
    for name in NAMES:
        if out[name] is None and saved.get(name) in free:
            out[name] = saved[name]
            free.remove(saved[name])
    for name in NAMES:
        if out[name] is None and free:
            out[name] = free.pop(0)
    return out


def jpeg_size(jpeg):
    """-> (width, height) from a JPEG's SOF header, or (None, None)."""
    i, n = 2, len(jpeg)
    while i + 9 < n:
        if jpeg[i] != 0xFF:
            i += 1
            continue
        marker = jpeg[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            i += 1 if marker == 0xFF else 2
            continue
        if marker == 0xDA:          # start of scan: no SOF before it
            break
        seg = struct.unpack(">H", jpeg[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", jpeg[i + 5:i + 9])
            return w, h
        i += 2 + seg
    return None, None


def pack_frame(header, jpeg):
    """4-byte header length, JSON header, JPEG -- as onboard/g1_camera.py."""
    h = json.dumps(header, separators=(",", ":")).encode()
    return struct.pack("<I", len(h)) + h + jpeg


class HandCamera:
    """One camera: a capture thread and the newest frame."""

    def __init__(self, name, path, width=640, height=480, fps=30.0,
                 opener=None, reencode=None, reopen_delay=2.0, reopen_max=30.0):
        self.name = name
        self.path = path
        self.width, self.height, self.fps = int(width), int(height), float(fps)
        self._opener = opener or self._default_opener
        self._reencode = reencode or self._default_reencode
        self.reopen_delay, self.reopen_max = float(reopen_delay), float(reopen_max)
        self._lock = threading.Lock()
        self._latest = None          # (header, jpeg)
        self._times = deque(maxlen=60)
        self.device = None
        self.camera_name = None
        self.opened = False
        self.seq = 0
        self.n_failed = 0
        self.n_open_failures = 0
        self.n_reencoded = 0
        self.last_error = None
        self._stop = threading.Event()
        self._thread = None

    # -- defaults: V4L2 through OpenCV -------------------------------------- #
    def _default_opener(self):
        import cv2
        dev = os.path.realpath(self.path)
        cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"cannot open {self.path} ({dev})")
        # MJPG: what UVC cameras deliver at 640x480/30 over USB 2. Raw YUYV
        # from two cameras on one shared hub can fail with "No space left on
        # device" (bandwidth), and MJPG is also what we pass through.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        # Hand back the camera's own JPEG bytes instead of decoding them.
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
        self.device = dev
        self.camera_name = _read(os.path.join(SYS_V4L, os.path.basename(dev), "name")) or None
        return cap

    @staticmethod
    def _default_reencode(img):
        import cv2
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            raise RuntimeError("jpeg encode failed")
        return buf.tobytes()

    def to_jpeg(self, buf):
        """The retrieved buffer -> JPEG bytes.

        With CONVERT_RGB off, OpenCV's V4L2 backend returns MJPG frames as one
        row of raw bytes. Some backends ignore the flag and return a decoded
        image; then it is encoded again (and counted, so /status shows it).
        """
        shape = getattr(buf, "shape", None)
        if shape is not None and len(shape) == 3:
            self.n_reencoded += 1
            return self._reencode(buf)
        data = buf.tobytes() if hasattr(buf, "tobytes") else bytes(buf)
        if not data.startswith(b"\xff\xd8"):
            raise RuntimeError("frame is not a JPEG")
        return data

    # -- lifecycle ----------------------------------------------------------- #
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name=f"cam-{self.name}")
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)

    def _run(self):
        cap = None
        delay = self.reopen_delay
        fails = 0
        while not self._stop.is_set():
            if cap is None:
                try:
                    cap = self._opener()
                    self.opened, fails, delay = True, 0, self.reopen_delay
                    print(f"[cameras] {self.name}: opened {self.path} -> {self.device}")
                except Exception as e:  # noqa: BLE001 -- keep trying, slowly
                    self.n_open_failures += 1
                    self.last_error = str(e)
                    self.opened = False
                    if self.n_open_failures in (1, 10) or self.n_open_failures % 100 == 0:
                        print(f"[cameras] {self.name}: {e}; retrying every {delay:.0f}s")
                    self._stop.wait(delay)
                    delay = min(self.reopen_max, delay * 2)
                    continue
            try:
                ok = cap.grab()
                t_mono, t_unix = time.monotonic_ns(), time.time_ns()
                ok, buf = cap.retrieve() if ok else (False, None)
                jpeg = self.to_jpeg(buf) if ok and buf is not None else None
            except Exception as e:  # noqa: BLE001
                jpeg = None
                self.last_error = str(e)
            if jpeg is None:
                self.n_failed += 1
                fails += 1
                if fails >= 30:
                    try:
                        cap.release()
                    except Exception:  # noqa: BLE001
                        pass
                    cap, self.opened = None, False
                    self._stop.wait(delay)
                    delay = min(self.reopen_max, delay * 2)
                continue
            fails = 0
            w, h = jpeg_size(jpeg)
            with self._lock:
                self.seq += 1
                self._latest = ({"cam": self.name, "seq": self.seq,
                                 "t_cap_mono_ns": t_mono, "t_cap_unix_ns": t_unix,
                                 "t": round(t_unix / 1e9, 4),
                                 "w": w, "h": h, "fmt": "jpeg",
                                 "port": usb_port(self.path)}, jpeg)
                self._times.append(t_mono)
        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass

    # -- readers ------------------------------------------------------------- #
    def latest(self):
        with self._lock:
            return self._latest

    def status(self):
        with self._lock:
            latest, times = self._latest, list(self._times)
        fps = ((len(times) - 1) * 1e9 / (times[-1] - times[0])
               if len(times) >= 2 and times[-1] > times[0] else 0.0)
        age = ((time.monotonic_ns() - latest[0]["t_cap_mono_ns"]) / 1e9
               if latest else None)
        return {"name": self.name, "path": self.path, "port": usb_port(self.path),
                "device": self.device, "camera": self.camera_name,
                "opened": self.opened, "frames": self.seq, "fps": round(fps, 1),
                "w": latest[0]["w"] if latest else None,
                "h": latest[0]["h"] if latest else None,
                "last_age_s": round(age, 3) if age is not None else None,
                "failed": self.n_failed, "open_failures": self.n_open_failures,
                "reencoded": self.n_reencoded, "last_error": self.last_error}


class HandCameras:
    """Both cameras, the left/right choice, and the Swap button."""

    def __init__(self, env=None, by_path_dir=BY_PATH, sys_dir=SYS_V4L, camera_cls=HandCamera):
        env = os.environ if env is None else env
        self.enabled = env.get("HAND_CAMERAS", "on").strip().lower() not in OFF
        self.left_cfg = normalize(env.get("HAND_CAM_LEFT", "auto"), by_path_dir)
        self.right_cfg = normalize(env.get("HAND_CAM_RIGHT", "auto"), by_path_dir)
        self.match = env.get("HAND_CAM_MATCH", "").strip()
        self.fps = float(env.get("HAND_CAM_FPS", 30))
        self.width = int(env.get("HAND_CAM_W", 640))
        self.height = int(env.get("HAND_CAM_H", 480))
        self.mapping_path = os.path.join(env.get("HAND_CAM_STATE_DIR", "/state"), "cameras.json")
        self._by_path_dir, self._sys_dir = by_path_dir, sys_dir
        self._camera_cls = camera_cls
        self._lock = threading.Lock()
        self.cameras = {}
        self.found = []
        if self.enabled:
            self._build()

    def _build(self):
        self.found = list_capture_nodes(self._by_path_dir, self._sys_dir)
        paths = assign(self.left_cfg, self.right_cfg, self.found, self.match,
                       load_mapping(self.mapping_path))
        for name in NAMES:
            if paths[name]:
                self.cameras[name] = self._camera_cls(
                    name, paths[name], self.width, self.height, self.fps)
        print(f"[cameras] found {len(self.found)} capture node(s); "
              + ", ".join(f"{n} = {paths[n] or 'none'}" for n in NAMES))

    def start(self):
        for cam in self.cameras.values():
            cam.start()
        return self

    def get(self, name):
        with self._lock:
            return self.cameras.get(name)

    def can_swap(self):
        """Only when both sides are auto: an explicit path is the operator's word."""
        return self.left_cfg == "auto" and self.right_cfg == "auto"

    def swap(self):
        if not self.can_swap():
            return False, "left and right are set explicitly; change HAND_CAM_LEFT/RIGHT"
        with self._lock:
            left, right = self.cameras.get("left"), self.cameras.get("right")
            if left is None or right is None:
                return False, "swap needs two cameras"
            left.name, right.name = "right", "left"
            self.cameras["left"], self.cameras["right"] = right, left
            mapping = {"left": right.path, "right": left.path}
        try:
            save_mapping(self.mapping_path, mapping)
        except OSError as e:
            return True, f"swapped, but not saved ({e})"
        print(f"[cameras] swapped: left = {mapping['left']}, right = {mapping['right']}")
        return True, "swapped"

    def status(self):
        with self._lock:
            cams = {n: c.status() for n, c in self.cameras.items()}
        return {"enabled": self.enabled, "can_swap": self.can_swap(),
                "fps_requested": self.fps,
                "found": [{"by_path": n["by_path"], "port": usb_port(n["by_path"]),
                           "dev": n["dev"], "name": n["name"]} for n in self.found],
                "cameras": cams}


# -- HTTP ---------------------------------------------------------------------- #
def make_app(cams):
    from aiohttp import web

    async def status(request):
        return web.json_response(cams.status())

    async def frame(request):
        cam = cams.get(request.match_info["name"])
        latest = cam.latest() if cam is not None else None
        if latest is None:
            return web.Response(status=404, text="no frame")
        header, jpeg = latest
        return web.Response(body=jpeg, content_type="image/jpeg", headers={
            "Cache-Control": "no-store",
            "X-Frame-Seq": str(header["seq"]),
            "X-Captured-Mono-Ns": str(header["t_cap_mono_ns"]),
            "X-Captured-Unix-Ns": str(header["t_cap_unix_ns"])})

    async def stream(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        name = request.match_info["name"]
        last = 0
        while not ws.closed:
            # Looked up every time, so a swap takes effect on open streams.
            cam = cams.get(name)
            latest = cam.latest() if cam is not None else None
            if latest is not None and (latest[0]["cam"], latest[0]["seq"]) != last:
                last = (latest[0]["cam"], latest[0]["seq"])
                try:
                    await ws.send_bytes(pack_frame(*latest))
                except Exception:  # noqa: BLE001 -- a dead socket ends the stream
                    break
            await asyncio.sleep(0.5 / max(1.0, cams.fps))
        return ws

    async def swap(request):
        ok, msg = cams.swap()
        return web.json_response({"ok": ok, "msg": msg}, status=200 if ok else 409)

    app = web.Application()
    app.router.add_get("/status", status)
    app.router.add_get("/health", status)
    app.router.add_get("/cam/{name}/frame.jpg", frame)
    app.router.add_get("/cam/{name}", stream)
    app.router.add_post("/swap", swap)
    return app


def main():
    from aiohttp import web
    cams = HandCameras()
    if not cams.enabled:
        print("[cameras] HAND_CAMERAS is off; serving status only")
    elif not cams.cameras:
        print("[cameras] no hand cameras found; serving status only")
    cams.start()
    port = int(os.environ.get("HAND_CAM_PORT", 8091))
    print(f"[cameras] serving on :{port}")
    web.run_app(make_app(cams), host="0.0.0.0", port=port, print=None)


if __name__ == "__main__":
    main()
