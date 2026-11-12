"""Head camera capture ON the robot, streamed to the bridge for the recorder.

Why here and not on the bridge: the camera is plugged into the robot's
computer, and a learned policy sees what the robot sees. An episode without
the camera records the operator's hands, the commands and the joints -- good
for debugging teleop, useless for imitation learning, which is what the
episodes are for.

Shape: one thread grabs frames at a fixed rate, JPEG-encodes them and keeps
only the newest. A second WebSocket, /cam, sends each new frame to whoever is
connected (the bridge). The pose socket, /ctrl, is untouched: its timing is
what the arms follow, and camera bytes do not belong on it.

Latest-wins, like the pose path. A queue of frames would deliver the past;
the recorder wants the frame that matches the pose row it is writing, and a
frame that arrives late is stamped with its capture time so replay can tell.

Off by G1_CAM=0. The OpenCV import is lazy and every failure is counted and
reported in /status rather than raised: a missing camera must never take the
commander down, because the commander is the only thing allowed to move the
arms and the camera is not.
"""
import asyncio
import json
import os
import struct
import threading
import time

from aiohttp import web


def pack_frame(header: dict, jpeg: bytes) -> bytes:
    """One binary WebSocket message: 4-byte header length, JSON header, JPEG.

    Mirrored by bridge/cam_client.py::unpack_frame. The two images do not
    share code (real/ is excluded from the bridge image on purpose), so a test
    packs with one and unpacks with the other.
    """
    h = json.dumps(header, separators=(",", ":")).encode()
    return struct.pack("<I", len(h)) + h + jpeg


class CameraSource:
    def __init__(self, device="0", width=640, height=480, hz=15.0, quality=80,
                 opener=None, encoder=None, reopen_after=10, reopen_delay=2.0):
        self.device = str(device)
        self.width, self.height = int(width), int(height)
        self.hz = float(hz)
        self.quality = int(quality)
        # Injected in tests; the defaults need cv2, imported only when called.
        self._opener = opener or self._default_opener
        self._encoder = encoder or self._default_encoder
        self.reopen_after = int(reopen_after)    # consecutive failed reads
        self.reopen_delay = float(reopen_delay)
        self._lock = threading.Lock()
        self._latest = None                      # (header, jpeg)
        self._latest_mono = None
        self.n_captured = 0
        self.n_failed = 0
        self.n_open_failures = 0
        self.last_error = None
        self.opened = False
        self._stop = threading.Event()
        self._thread = None

    @classmethod
    def from_env(cls, env=None, **kw):
        """-> a CameraSource from G1_CAM* variables, or None when G1_CAM=0
        or G1_CAMERA=none (a robot without a head camera: without this it
        retried a missing /dev/video4 every 2 s and recorded 0 frames)."""
        env = os.environ if env is None else env
        if env.get("G1_CAM", "1") in ("0", "off", "no", "false"):
            return None
        if str(env.get("G1_CAMERA", "0")).strip().lower() in ("none", "off", ""):
            return None
        return cls(device=env.get("G1_CAMERA", "0"),
                   width=int(env.get("G1_CAM_W", 640)),
                   height=int(env.get("G1_CAM_H", 480)),
                   hz=float(env.get("G1_CAM_HZ", 15)),
                   quality=int(env.get("G1_CAM_JPEG_Q", 80)), **kw)

    # -- defaults: V4L2 through OpenCV -------------------------------------- #
    def _default_opener(self):
        import cv2
        dev = int(self.device) if self.device.isdigit() else self.device
        cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
        # Ask for MJPG from the camera: most UVC cameras deliver 640x480 at
        # 30 fps only in MJPG, and the decode is cheaper than a raw grab.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.hz)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"cannot open camera {self.device}")
        return cap

    def _default_encoder(self, img, quality):
        import cv2
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            raise RuntimeError("jpeg encode failed")
        return buf.tobytes()

    # -- lifecycle ----------------------------------------------------------- #
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="camera")
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)

    def _run(self):
        cap = None
        fails = 0
        period = 1.0 / self.hz if self.hz > 0 else 0.0
        next_t = time.monotonic()
        while not self._stop.is_set():
            if cap is None:
                try:
                    cap = self._opener()
                    self.opened = True
                    fails = 0
                except Exception as e:  # noqa: BLE001 -- a camera must never kill the commander
                    self.n_open_failures += 1
                    self.last_error = str(e)
                    self.opened = False
                    self._stop.wait(self.reopen_delay)
                    continue
            now = time.monotonic()
            if now < next_t:
                self._stop.wait(next_t - now)
                continue
            # Pace to the rate; if a read ran long, resync rather than burst.
            next_t = max(next_t + period, now)
            try:
                ok, img = cap.read()
            except Exception as e:  # noqa: BLE001
                ok, img = False, None
                self.last_error = str(e)
            if not ok or img is None:
                self.n_failed += 1
                fails += 1
                if fails >= self.reopen_after:
                    try:
                        cap.release()
                    except Exception:  # noqa: BLE001
                        pass
                    cap = None
                    self.opened = False
                    self._stop.wait(self.reopen_delay)
                continue
            fails = 0
            try:
                jpeg = self._encoder(img, self.quality)
            except Exception as e:  # noqa: BLE001
                self.n_failed += 1
                self.last_error = str(e)
                continue
            h, w = img.shape[:2]
            with self._lock:
                self.n_captured += 1
                self._latest = ({"seq": self.n_captured, "t": time.time(),
                                 "w": int(w), "h": int(h), "fmt": "jpeg"}, jpeg)
                self._latest_mono = time.monotonic()
        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass

    # -- readers ------------------------------------------------------------- #
    def latest(self):
        """-> (header, jpeg) of the newest frame, or None."""
        with self._lock:
            return self._latest

    def status(self):
        with self._lock:
            age = (time.monotonic() - self._latest_mono) if self._latest_mono else None
        return {"device": self.device, "w": self.width, "h": self.height, "hz": self.hz,
                "quality": self.quality, "opened": self.opened,
                "captured": self.n_captured, "failed": self.n_failed,
                "open_failures": self.n_open_failures, "last_error": self.last_error,
                "last_age_s": round(age, 3) if age is not None else None}


async def stream_camera(ws, source, hz=None):
    """Send every NEW frame to `ws` until it closes. Never the same seq twice."""
    period = 1.0 / float(hz or source.hz or 15.0)
    last = 0
    while not ws.closed:
        item = source.latest()
        if item is not None and item[0]["seq"] != last:
            last = item[0]["seq"]
            try:
                await ws.send_bytes(pack_frame(*item))
            except Exception:  # noqa: BLE001 -- a dead socket ends the stream
                return
        await asyncio.sleep(period)


async def cam_ws_handler(request, ws_factory=web.WebSocketResponse):
    """GET /cam?key=... : the head camera as a stream of packed JPEG frames."""
    ws = ws_factory()
    await ws.prepare(request)
    secret = request.app.get("secret") or ""
    if secret and request.query.get("key") != secret:
        await ws.send_json({"error": "bad key"})
        await ws.close()
        return ws
    source = request.app.get("camera")
    if source is None:
        await ws.send_json({"error": "no camera"})
        await ws.close()
        return ws
    await stream_camera(ws, source)
    return ws
