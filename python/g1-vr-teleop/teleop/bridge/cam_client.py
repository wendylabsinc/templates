"""The bridge's end of the robot's camera stream (real/g1_camera.py).

Connects to the onboard's /cam WebSocket, keeps the newest JPEG frame, hands
every frame to the session recorder, and answers two questions for the pose
path: which camera frame is current right now (seq, capture time, age), and
is the camera alive at all (/control/status).

Own thread and event loop, like the sink, so a camera hiccup cannot touch the
pose frames. Reconnects forever; the recorder simply gets no frames while it
is down and the pose rows say so through the age.
"""
import asyncio
import json
import struct
import threading
import time
from collections import deque
from urllib.parse import urlsplit, urlunsplit

import aiohttp


def unpack_frame(blob):
    """-> (header, jpeg) or None. Mirrors real/g1_camera.py::pack_frame."""
    try:
        if len(blob) < 4:
            return None
        n = struct.unpack("<I", blob[:4])[0]
        if len(blob) < 4 + n:
            return None
        header = json.loads(blob[4:4 + n])
        if not isinstance(header, dict):
            return None
        return header, bytes(blob[4 + n:])
    except (struct.error, ValueError, UnicodeDecodeError):
        return None


def cam_url_from_ctrl(url: str) -> str:
    """ws://robot:8090/ctrl?key=k -> ws://robot:8090/cam?key=k"""
    p = urlsplit(url)
    path = p.path[:-len("/ctrl")] + "/cam" if p.path.endswith("/ctrl") else p.path.rstrip("/") + "/cam"
    return urlunsplit((p.scheme, p.netloc, path, p.query, p.fragment))


class CameraClient:
    def __init__(self, url, on_frame=None):
        self._init_state(url, on_frame)
        threading.Thread(target=self._thread, daemon=True, name="cam-client").start()

    def _init_state(self, url, on_frame):
        self.url = url
        self.on_frame = on_frame
        self._lock = threading.Lock()
        self._latest = None            # (header, jpeg, t_recv monotonic)
        self._times = deque(maxlen=60)
        self.n_frames = 0
        self.n_bytes = 0
        self.n_bad = 0
        self.connected = False

    def _handle(self, blob):
        item = unpack_frame(blob)
        if item is None:
            self.n_bad += 1
            return
        header, jpeg = item
        now = time.monotonic()
        with self._lock:
            self._latest = (header, jpeg, now)
            self._times.append(now)
            self.n_frames += 1
            self.n_bytes += len(jpeg)
        if self.on_frame is not None:
            try:
                self.on_frame(header, jpeg)
            except Exception as e:  # noqa: BLE001 -- the recorder must not break the stream
                print(f"[camera] on_frame error: {e}")

    def latest_ref(self):
        """-> {seq, t, age_s} of the newest frame, for the pose row, or None."""
        with self._lock:
            if self._latest is None:
                return None
            header, _, t_recv = self._latest
            return {"seq": header.get("seq"), "t": header.get("t"),
                    "age_s": round(time.monotonic() - t_recv, 4)}

    def status(self):
        with self._lock:
            latest = self._latest
            times = list(self._times)
        fps = ((len(times) - 1) / (times[-1] - times[0])
               if len(times) >= 2 and times[-1] > times[0] else 0.0)
        return {"connected": self.connected,
                "url": self.url.split("?")[0],       # never echo the key
                "frames": self.n_frames, "bytes": self.n_bytes, "bad": self.n_bad,
                "fps": round(fps, 1),
                "w": latest[0].get("w") if latest else None,
                "h": latest[0].get("h") if latest else None,
                "last_age_s": (round(time.monotonic() - latest[2], 3) if latest else None)}

    # -- io loop --------------------------------------------------------------- #
    def _thread(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._run())

    async def _run(self):
        while True:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.ws_connect(self.url, max_msg_size=8 * 1024 * 1024) as ws:
                        self.connected = True
                        print("[camera] connected to the onboard camera stream")
                        async for msg in ws:
                            if msg.type is aiohttp.WSMsgType.BINARY:
                                self._handle(msg.data)
                            elif msg.type is aiohttp.WSMsgType.TEXT:
                                print(f"[camera] onboard says: {msg.data[:120]}")
            except Exception as e:  # noqa: BLE001
                print(f"[camera] connection lost ({e}); retrying in 2s")
            finally:
                self.connected = False
            await asyncio.sleep(2)


def camera_frame_url(url: str) -> str:
    """Camera service base URL -> its direct JPEG endpoint."""
    p = urlsplit(url)
    path = p.path if p.path.rstrip("/").endswith("/frame.jpg") \
        else p.path.rstrip("/") + "/frame.jpg"
    return urlunsplit((p.scheme, p.netloc, path, p.query, ""))


def camera_health_url(url: str) -> str:
    p = urlsplit(camera_frame_url(url))
    return urlunsplit((p.scheme, p.netloc, "/health", p.query, ""))


class CameraServiceClient(CameraClient):
    """Persistent direct JPEG ingest from the single-owner camera service.

    This client keeps one HTTP
    connection to `/frame.jpg`, discards repeated snapshots by camera frame
    identity, and performs no detection work. Polling at 30 Hz is intentional:
    it catches a 15 Hz sensor without overlapping requests.
    """

    def __init__(self, url, on_frame=None, hz=30.0):
        self._init_state(camera_frame_url(url), on_frame)
        self.period = 1.0 / max(0.5, float(hz))
        self._seq = 0
        self._last_identity = None
        self.width = None
        self.height = None
        self.stream_id = None
        threading.Thread(target=self._thread, daemon=True,
                         name="cam-service").start()

    async def _read_health(self, session):
        async with session.get(camera_health_url(self.url)) as response:
            response.raise_for_status()
            health = await response.json()
        self.width = health.get("width")
        self.height = health.get("height")
        self.stream_id = health.get("rgbd_stream_id")

    def _handle_jpeg(self, jpeg, *, frame_id, captured_at_unix_ns):
        if not isinstance(jpeg, (bytes, bytearray)) or not jpeg.startswith(b"\xff\xd8\xff"):
            self.n_bad += 1
            return False
        identity = (self.stream_id, frame_id, captured_at_unix_ns)
        if identity == self._last_identity:
            return False
        self._last_identity = identity
        self._seq += 1
        ns = captured_at_unix_ns or time.time_ns()
        header = {"seq": self._seq, "t": round(float(ns) / 1e9, 4),
                  "w": self.width, "h": self.height,
                  "frame_id": frame_id, "src": "camera",
                  "stream_id": self.stream_id,
                  "captured_at_unix_ns": ns,
                  "received_at_unix_ns": time.time_ns(),
                  "received_at_monotonic_ns": time.monotonic_ns()}
        now = time.monotonic()
        with self._lock:
            self._latest = (header, bytes(jpeg), now)
            self._times.append(now)
            self.n_frames += 1
            self.n_bytes += len(jpeg)
        if self.on_frame is not None:
            try:
                self.on_frame(header, bytes(jpeg))
            except Exception as error:  # noqa: BLE001 -- never break live video
                print(f"[camera] on_frame error: {error}")
        return True

    async def _run(self):
        timeout = aiohttp.ClientTimeout(total=2.0)
        while True:
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    await self._read_health(session)
                    while True:
                        started = time.monotonic()
                        async with session.get(self.url) as response:
                            response.raise_for_status()
                            jpeg = await response.read()
                            frame_id = int(response.headers.get("X-Frame-Id", "0"))
                            captured = int(response.headers.get(
                                "X-Captured-At-Unix-Ns", "0"))
                        self.connected = True
                        self._handle_jpeg(jpeg, frame_id=frame_id,
                                          captured_at_unix_ns=captured)
                        await asyncio.sleep(max(0.0, self.period -
                                                (time.monotonic() - started)))
            except Exception as error:  # noqa: BLE001 -- camera loss is recoverable
                self.connected = False
                print(f"[camera] direct stream lost ({type(error).__name__}); retrying in 1s")
                await asyncio.sleep(1)

