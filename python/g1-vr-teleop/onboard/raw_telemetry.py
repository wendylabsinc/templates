"""Passive copies of messages already read/written by the onboard process.

Nothing here publishes to DDS, changes a command, or claims robot ownership.
No subscriber means no snapshots. A slow archive loses counted readings,
never holds up the control loop waiting for network or disk.
"""
from collections import deque
from dataclasses import fields, is_dataclass
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
import json
import multiprocessing
import threading
import time
import uuid


def joint_layout():
    legs = [f"{side}_{joint}_joint" for side in ("left", "right")
            for joint in ("hip_pitch", "hip_roll", "hip_yaw", "knee", "ankle_pitch", "ankle_roll")]
    waist = [f"waist_{axis}_joint" for axis in ("yaw", "roll", "pitch")]
    arms = [f"{side}_{joint}_joint" for side in ("left", "right") for joint in (
        "shoulder_pitch", "shoulder_roll", "shoulder_yaw", "elbow", "wrist_roll", "wrist_pitch", "wrist_yaw")]
    hands = {side: [f"{side}_hand_{joint}_joint" for joint in joints] for side, joints in (
        ("left", ("thumb_0", "thumb_1", "thumb_2", "middle_0", "middle_1", "index_0", "index_1")),
        ("right", ("thumb_0", "thumb_1", "thumb_2", "index_0", "index_1", "middle_0", "middle_1")))}
    return {"id": "unitree-g1-29-dex3-v1", "body_joint_names": legs + waist + arms,
            "hand_motor_joint_names": hands,
            "units": {"q": "rad", "dq": "rad/s", "tau_est": "N m (estimated)"},
            "hand_identity": "SDK topic side; no assumption about operator physical handedness"}


@lru_cache(maxsize=128)
def _field_names(cls):
    return tuple(field.name for field in fields(cls))


def plain(value):
    # Most leaves are built-in numbers. Reflection at every leaf added work
    # to every lowstate callback (hundreds per second) and every SDK write.
    kind = type(value)
    if kind in (str, float, int, bool, type(None)):
        return value
    if kind in (bytes, bytearray):
        return list(value)
    if kind in (list, tuple):
        return [plain(v) for v in value]
    if kind is dict:
        return {str(k): plain(v) for k, v in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return {name: plain(getattr(value, name)) for name in _field_names(kind)}
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, bytes, bytearray)):
        return [plain(v) for v in value]
    if hasattr(value, "tolist"):
        return plain(value.tolist())
    if value is None or isinstance(value, (str, float, int, bool)):
        return value
    raise TypeError(f"unsupported reading type: {type(value).__name__}")


def encode_readings_batch(batch):
    """Runs in a separate process: JSON must not occupy the control loop/GIL."""
    return json.dumps(batch)


def _encode_in_process(executor, batch):
    """Submit/wait from a helper thread so process startup misses aiohttp too."""
    return executor.submit(encode_readings_batch, batch).result()


class RawTelemetry:
    def __init__(self, capacity=512):
        self.readings = deque(maxlen=capacity)
        self.lock = threading.Lock()
        self.subscribers = 0
        self.sequence = 0
        self.errors = 0
        self.boot_id = uuid.uuid4().hex
        self.joint_layout = joint_layout()
        self.deferred_drops = 0
        self._deferred = deque(maxlen=capacity)
        self._deferred_lock = threading.Lock()
        self._deferred_wake = threading.Event()
        self._deferred_thread = None
        self._deferred_stop = False

    def start_worker(self):
        """Start the snapshot worker before the first hot-path submission."""
        if self._deferred_thread is None or not self._deferred_thread.is_alive():
            self._deferred_stop = False
            self._deferred_thread = threading.Thread(
                target=self._run_deferred, name="raw-telemetry-snapshot", daemon=True)
            self._deferred_thread.start()

    def close(self):
        self._deferred_stop = True
        self._deferred_wake.set()
        if self._deferred_thread is not None:
            self._deferred_thread.join(timeout=1.0)

    def _append(self, stream, payload, unix_ns, monotonic_ns):
        try:
            record = {"stream": stream, "source_at_unix_ns": unix_ns,
                      "source_at_monotonic_ns": monotonic_ns,
                      "payload": plain(payload)}
            with self.lock:
                self.sequence += 1
                record["sequence"] = self.sequence
                self.readings.append(record)
        except Exception:  # a recorder must not change the motor path
            self.errors += 1

    def _run_deferred(self):
        while not self._deferred_stop:
            self._deferred_wake.wait(0.2)
            while not self._deferred_stop:
                with self._deferred_lock:
                    item = self._deferred.popleft() if self._deferred else None
                    if not self._deferred:
                        self._deferred_wake.clear()
                if item is None:
                    break
                stream, payload, unix_ns, monotonic_ns = item
                if (isinstance(payload, tuple) and len(payload) == 3
                        and payload[0] == "lowcmd-values-v1"):
                    payload = self._lowcmd_from_values(payload[1], payload[2])
                self._append(stream, payload, unix_ns, monotonic_ns)

    def capture_async(self, stream, payload):
        """Enqueue an unmodified, non-reused DDS reading for helper snapshotting."""
        if not self.subscribers:
            return
        item = (stream, payload, time.time_ns(), time.monotonic_ns())
        with self._deferred_lock:
            if len(self._deferred) == self._deferred.maxlen:
                self.deferred_drops += 1
            self._deferred.append(item)
        self._deferred_wake.set()

    @staticmethod
    def _lowcmd_from_values(values, write_result):
        values = list(values)
        motors = []
        offset = 2
        names = ("mode", "q", "dq", "tau", "kp", "kd", "reserve")
        for _ in range(35):
            motors.append(dict(zip(names, values[offset:offset + 7])))
            offset += 7
        message = {"mode_pr": values[0], "mode_machine": values[1],
                   "motor_cmd": motors, "reserve": values[offset:offset + 4],
                   "crc": values[offset + 4]}
        return {"message": message, "write_result": write_result}

    def capture_lowcmd(self, values, write_result):
        """Queue the immutable packed-command values already produced by CRC."""
        if not self.subscribers:
            return
        # The tuple is detached from the LowCmd object, which is mutated and
        # reused on the next motor tick. Expansion happens only in the worker.
        self.capture_async("rt/arm_sdk.write",
                           ("lowcmd-values-v1", tuple(values), write_result))

    def capture(self, stream, payload):
        if not self.subscribers:
            return
        self._append(stream, payload, time.time_ns(), time.monotonic_ns())

    def since(self, after):
        with self.lock:
            rows = [r for r in self.readings if r["sequence"] > after][:64]
            missing = max(0, rows[0]["sequence"] - after - 1) if rows else 0
            return {"schema": "wendy.g1.onboard-readings.v1", "boot_id": self.boot_id,
                    "joint_layout": self.joint_layout,
                    "readings": rows, "missing": missing, "capture_errors": self.errors,
                    "deferred_drops": self.deferred_drops,
                    "last_sequence": rows[-1]["sequence"] if rows else after}

    def status(self):
        with self._deferred_lock:
            queued = len(self._deferred)
        return {"subscribers": self.subscribers, "snapshot_queue": queued,
                "deferred_drops": self.deferred_drops,
                "capture_errors": self.errors, "sequence": self.sequence}


async def readings_ws_handler(request):
    import asyncio
    from aiohttp import web
    secret = request.app["secret"]
    if secret and request.query.get("key") != secret:
        raise web.HTTPUnauthorized()
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    log = request.app["cmd"].telemetry
    log.start_worker()
    cursor = log.sequence
    log.subscribers += 1
    # Read the socket too, so a normal peer close stops collection promptly.
    async def receive():
        async for _ in ws:
            pass
    reader = asyncio.create_task(receive())
    # Spawn, never fork a process containing live DDS threads. Only already
    # snapshotted primitive values enter this worker; it has no DDS handles.
    # One awaited batch bounds work in flight. A slow encoder is visible via
    # the existing ring's missing counter, rather than delaying commands.
    encoder = None
    try:
        encoder = await asyncio.to_thread(
            ProcessPoolExecutor, max_workers=1,
            mp_context=multiprocessing.get_context("spawn"))
        first_batch = True
        last_errors = -1
        while not ws.closed and not reader.done():
            batch = log.since(cursor)
            if (not first_batch and not batch["readings"]
                    and batch["capture_errors"] == last_errors):
                await asyncio.sleep(0.02)
                continue
            # Both process submission/startup and its result wait live on a
            # helper thread. A cold spawn or slow JSON batch cannot occupy the
            # aiohttp loop that receives the robot's motion commands.
            wire = await asyncio.to_thread(_encode_in_process, encoder, batch)
            if ws.closed or reader.done():
                break
            await ws.send_str(wire)
            cursor = batch["last_sequence"]
            first_batch = False
            last_errors = batch["capture_errors"]
            await asyncio.sleep(0.02)
    except Exception:
        log.errors += 1
        await ws.close(code=1011, message=b"telemetry encoding or transport failed")
    finally:
        log.subscribers -= 1
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        # shutdown(wait=True) would put a slow worker back on the control
        # loop at disconnect. Let the one running batch finish independently.
        if encoder is not None:
            encoder.shutdown(wait=False, cancel_futures=True)
    return ws


async def readings_clock_handler(request):
    """Passive clock observation. It never adjusts either machine's clock."""
    from aiohttp import web
    received = time.time_ns()
    secret = request.app["secret"]
    if secret and request.query.get("key") != secret:
        raise web.HTTPUnauthorized()
    return web.json_response({"boot_id": request.app["cmd"].telemetry.boot_id,
        "source_receive_unix_ns": received,
        "source_monotonic_ns": time.monotonic_ns(), "source_send_unix_ns": time.time_ns()})
