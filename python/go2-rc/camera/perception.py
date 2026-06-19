"""Lidar perception subscriber for go2-camera.

Subscribes directly to the dog's onboard lidar topic (`/utlidar/cloud_deskewed`)
over CycloneDDS — no ROS2 / colcon needed. Derives two things and shares
them via a thread-safe `PerceptionState`:

  - `free_space`: 36-sector min-range polar map (for the proximity warning
    in go2-RC's vignette)
  - `scan_xy`: downsampled 2D points in dog's base_link frame (for the
    top-right radar-style canvas in go2-RC)

Topic conventions match watchtower: ROS2-on-Cyclone uses `rt/<topic>`
on the wire, so `/utlidar/cloud_deskewed` becomes
`rt/utlidar/cloud_deskewed`. QoS for lidar must be BEST_EFFORT — the
Unitree driver won't deliver to a RELIABLE subscriber.

This file MUST NOT use `from __future__ import annotations`. cyclonedds's
IdlStruct normalizer resolves type hints by name lookup at class
definition time; PEP-563 string annotations break it the same way they
break go2-brain's perception.py.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from cyclonedds.core import Policy, Qos
from cyclonedds.domain import DomainParticipant
from cyclonedds.idl import IdlStruct
from cyclonedds.idl.annotations import keylist
from cyclonedds.idl.types import sequence, uint8, uint32, int32
from cyclonedds.sub import DataReader, Subscriber
from cyclonedds.topic import Topic


log = logging.getLogger("go2-camera.perception")


# --- Tunables ---------------------------------------------------------------
LIDAR_TOPIC = os.environ.get("LIDAR_TOPIC", "rt/utlidar/cloud_deskewed")
# Height-filter bounds are FLOOR-RELATIVE: 5 cm above floor to 60 cm
# above floor by default — covers the slab where collision-relevant
# obstacles live for a quadruped (dog body height ~40 cm).
HEIGHT_MIN_M = float(os.environ.get("LIDAR_HEIGHT_MIN_M", "0.05"))
HEIGHT_MAX_M = float(os.environ.get("LIDAR_HEIGHT_MAX_M", "0.60"))
# The Livox MID-360 publishes points in the LIDAR's own frame (origin
# at the sensor, ~0.40 m above the floor on a standing Go2 EDU). To make
# the floor-relative bounds above mean what they say, we add this offset
# to z before the height filter. Set to 0 if your firmware happens to
# publish the cloud in base_link instead. The startup diagnostic logs
# the z-range of the first scan so you can tell which frame you're in:
# z centered around 0 → LIDAR frame (default offset is right);
# z biased ~+0.4 m   → already base_link (set offset to 0).
LIDAR_HEIGHT_OFFSET_M = float(os.environ.get("LIDAR_HEIGHT_OFFSET_M", "0.40"))
# Lateral mount offsets: where the LIDAR sits relative to the dog's body
# center, in the dog's own frame. +X_OFFSET = LIDAR sits forward of body
# center; +Y_OFFSET = LIDAR sits to the dog's left of body center. The
# Livox MID-360 publishes points in its own frame, so we add these to
# every point to translate into body-centered coordinates — the radar's
# triangle (drawn at canvas center) then represents the dog's body, not
# the sensor. Bigger numbers = bigger visual shift; tune until the
# triangle sits in the geometric center of the dot cloud when the dog
# is in a closed room.
LIDAR_X_OFFSET_M = float(os.environ.get("LIDAR_X_OFFSET_M", "0.20"))
LIDAR_Y_OFFSET_M = float(os.environ.get("LIDAR_Y_OFFSET_M", "-0.10"))
RANGE_MIN_M = float(os.environ.get("LIDAR_RANGE_MIN_M", "0.30"))
RANGE_MAX_M = float(os.environ.get("LIDAR_RANGE_MAX_M", "5.00"))
SECTOR_DEG = float(os.environ.get("LIDAR_SECTOR_DEG", "10.0"))
# How many points to forward to the browser per scan. The Livox MID-360
# emits 100k+ per scan — we downsample randomly to keep the JSON payload
# under ~10 KB. 500 is enough to sketch room geometry on a 250 px canvas.
SCAN_BROWSER_POINTS = int(os.environ.get("SCAN_BROWSER_POINTS", "500"))
DDS_DOMAIN = int(os.environ.get("DDS_DOMAIN", "0"))


# --- IDL definitions for ROS2 sensor_msgs/PointCloud2 and friends -----------
# Typenames must match ROS2's wire format exactly: `<pkg>::msg::dds_::<Type>_`.
# Field types (uint32, sequences, etc) need to come from cyclonedds.idl.types
# rather than Python builtins, otherwise the IDL serializer treats them
# as native int (which is signed 64-bit and won't match the wire format).


@dataclass
class _Time(IdlStruct, typename="builtin_interfaces::msg::dds_::Time_"):
    sec: int32 = 0
    nanosec: uint32 = 0


@dataclass
class _Header(IdlStruct, typename="std_msgs::msg::dds_::Header_"):
    stamp: _Time = field(default_factory=_Time)
    frame_id: str = ""


@dataclass
class _PointField(IdlStruct, typename="sensor_msgs::msg::dds_::PointField_"):
    name: str = ""
    offset: uint32 = 0
    datatype: uint8 = 0
    count: uint32 = 0


@dataclass
class _PointCloud2(IdlStruct, typename="sensor_msgs::msg::dds_::PointCloud2_"):
    header: _Header = field(default_factory=_Header)
    height: uint32 = 0
    width: uint32 = 0
    fields: sequence[_PointField] = field(default_factory=list)
    is_bigendian: bool = False
    point_step: uint32 = 0
    row_step: uint32 = 0
    data: sequence[uint8] = field(default_factory=list)
    is_dense: bool = False


# --- Pure-numpy helpers (ported from watchtower's go2_lidar_filter.py) ------


def _parse_pointcloud2(msg: _PointCloud2) -> np.ndarray:
    """Return an (N, 3) float32 (x,y,z) array from a PointCloud2.

    Reads the structured byte buffer directly with numpy. For a 100k-point
    Livox scan this runs in a few ms; the per-point Python iterator approach
    would take ~50 ms.
    """
    n_total = msg.width * msg.height
    if n_total == 0 or not msg.fields or msg.point_step == 0:
        return np.zeros((0, 3), dtype=np.float32)

    # cyclonedds-python decodes `sequence[uint8]` to a Python list/bytes;
    # frombuffer needs a bytes-like with a c-contiguous buffer.
    raw = np.frombuffer(bytes(msg.data), dtype=np.uint8)
    if raw.size < n_total * msg.point_step:
        # Truncated message — happens occasionally on a flaky link.
        n_total = raw.size // msg.point_step
        if n_total == 0:
            return np.zeros((0, 3), dtype=np.float32)

    fields = {f.name: f for f in msg.fields}
    if not all(k in fields for k in ("x", "y", "z")):
        raise ValueError(
            f"PointCloud2 missing x/y/z fields; got {list(fields)}"
        )
    if any(fields[k].datatype != 7 for k in ("x", "y", "z")):
        # datatype 7 == FLOAT32 in sensor_msgs/PointField.
        raise ValueError("PointCloud2 x/y/z must be FLOAT32")

    rows = raw[: n_total * msg.point_step].reshape(n_total, msg.point_step)
    out = np.empty((n_total, 3), dtype=np.float32)
    for i, name in enumerate(("x", "y", "z")):
        off = fields[name].offset
        out[:, i] = rows[:, off : off + 4].copy().view(np.float32).ravel()
    return out


def _scan_to_sectors(
    pts: np.ndarray, n_sectors: int, sector_deg: float, max_range_m: float,
) -> np.ndarray:
    """Vectorized: filter by height + range → bin by bearing → per-sector min."""
    if pts.size == 0:
        return np.full(n_sectors, max_range_m, dtype=np.float32)
    # Convert LIDAR-frame z to floor-relative before applying the height
    # bounds. Offset defaults to the Go2 EDU's standing LIDAR height; see
    # `LIDAR_HEIGHT_OFFSET_M` notes above for the base_link case.
    z = pts[:, 2] + LIDAR_HEIGHT_OFFSET_M
    height_mask = (z > HEIGHT_MIN_M) & (z < HEIGHT_MAX_M)
    pts = pts[height_mask]
    if pts.size == 0:
        return np.full(n_sectors, max_range_m, dtype=np.float32)
    # LIDAR → body frame translation: the sensor isn't at the body's
    # geometric center, so each point is shifted by the mount offset.
    # Range + bearing are then computed in body-centered coords, which
    # is what brain's safety wrapper expects when querying "what's
    # ahead of the DOG?" (not "ahead of the SENSOR").
    x = pts[:, 0] + LIDAR_X_OFFSET_M
    y = pts[:, 1] + LIDAR_Y_OFFSET_M
    rng = np.hypot(x, y)
    range_mask = (rng > RANGE_MIN_M) & (rng < max_range_m)
    x, y, rng = x[range_mask], y[range_mask], rng[range_mask]
    if x.size == 0:
        return np.full(n_sectors, max_range_m, dtype=np.float32)
    bearing_deg = np.degrees(np.arctan2(y, x))
    idx = ((bearing_deg + 180.0) / sector_deg).astype(np.int32)
    np.clip(idx, 0, n_sectors - 1, out=idx)
    sector_min = np.full(n_sectors, max_range_m, dtype=np.float32)
    np.minimum.at(sector_min, idx, rng)
    return sector_min


def _scan_for_browser(pts: np.ndarray, max_points: int) -> np.ndarray:
    """Filter + downsample raw cloud to a small (N, 2) array for canvas display."""
    if pts.size == 0:
        return np.zeros((0, 2), dtype=np.float32)
    # Same offset as `_scan_to_sectors` — converts LIDAR-frame z to
    # floor-relative so the bounds match the docstring intent.
    z = pts[:, 2] + LIDAR_HEIGHT_OFFSET_M
    height_mask = (z > HEIGHT_MIN_M) & (z < HEIGHT_MAX_M)
    xy = pts[height_mask, :2].copy()
    if xy.size == 0:
        return np.zeros((0, 2), dtype=np.float32)
    # Translate LIDAR-frame x/y into body-centered coords. The radar
    # canvas draws the dog (triangle) at center (0,0); this shift makes
    # that center coincide with the dog's body, not the sensor mount.
    xy[:, 0] += LIDAR_X_OFFSET_M
    xy[:, 1] += LIDAR_Y_OFFSET_M
    rng = np.hypot(xy[:, 0], xy[:, 1])
    range_mask = (rng > RANGE_MIN_M) & (rng < RANGE_MAX_M)
    xy = xy[range_mask]
    if xy.shape[0] > max_points:
        # Stride-sample rather than np.random.choice — ~5x faster, no RNG state.
        stride = xy.shape[0] // max_points
        xy = xy[::stride][:max_points]
    return xy.astype(np.float32, copy=False)


# --- State + worker --------------------------------------------------------


@dataclass
class PerceptionSnapshot:
    """A point-in-time read of the perception state. Plain data — no DDS."""
    stamp_ns: int = 0
    free_space: List[float] = field(default_factory=list)
    free_space_min_m: float = float("inf")
    free_space_min_bearing_deg: float = 0.0
    sector_deg: float = SECTOR_DEG
    scan_xy: List[List[float]] = field(default_factory=list)
    n_raw_points: int = 0
    have_data: bool = False


class PerceptionState:
    """Thread-safe latest snapshot. Producer = DDS reader thread,
    consumer = FastAPI WebSocket handlers (any number)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap = PerceptionSnapshot()

    def update(self, snap: PerceptionSnapshot) -> None:
        with self._lock:
            self._snap = snap

    def latest(self) -> PerceptionSnapshot:
        with self._lock:
            return self._snap


class LidarSubscriber:
    """Background thread: reads PointCloud2 samples, updates PerceptionState.

    QoS: BEST_EFFORT/KEEP_LAST(4), matching the Unitree driver. RELIABLE
    subscribers get nothing on this firmware.
    """

    def __init__(self, state: PerceptionState, domain: int = DDS_DOMAIN) -> None:
        self._state = state
        self._domain = domain
        self._n_sectors = int(round(360.0 / SECTOR_DEG))
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="lidar-sub", daemon=True
        )
        self._got_first = False
        self._last_warn_t = 0.0
        # Track time-since-last-sample so we can yell about a misconfigured
        # LIDAR_TOPIC. The subscriber sets up cleanly even with the wrong
        # topic name, so without this hint the operator just sees "lidar
        # · — m" forever with no log line pointing at the cause.
        self._last_sample_t = time.monotonic()
        self._last_no_data_warn_t = 0.0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            dp = DomainParticipant(self._domain)
            qos = Qos(
                Policy.Reliability.BestEffort,
                Policy.History.KeepLast(4),
            )
            topic = Topic(dp, LIDAR_TOPIC, _PointCloud2, qos=qos)
            sub = Subscriber(dp)
            reader = DataReader(sub, topic, qos=qos)
            log.info(
                "lidar subscriber up: topic=%s, domain=%d, BEST_EFFORT, "
                "%d sectors x %.1f°", LIDAR_TOPIC, self._domain,
                self._n_sectors, SECTOR_DEG,
            )
        except Exception:
            log.exception("Failed to set up lidar DDS subscriber")
            return

        # cyclonedds-python's `take_iter` blocks until samples arrive (with
        # an internal poll). We loop forever; the thread is daemon so
        # process exit kills it. Between iterations we check whether any
        # sample has actually arrived recently — silence past WARN_AFTER_S
        # almost always means the configured LIDAR_TOPIC doesn't match the
        # firmware, since DDS setup itself doesn't fail when the topic
        # exists but has no publishers.
        WARN_AFTER_S = 5.0       # first warning when this long without samples
        WARN_INTERVAL_S = 10.0   # repeat every this long while still silent
        while not self._stop.is_set():
            try:
                for sample in reader.take_iter(timeout=1_000_000_000):  # 1 s
                    self._handle(sample)
            except Exception:
                # Any exception out of take_iter is rare but recoverable —
                # back off and retry rather than tearing down.
                log.exception("lidar reader error; retrying")
                time.sleep(0.5)
                continue
            # Reached when take_iter returned (timeout or zero samples).
            now = time.monotonic()
            silence = now - self._last_sample_t
            if silence > WARN_AFTER_S and (
                now - self._last_no_data_warn_t > WARN_INTERVAL_S
            ):
                log.warning(
                    "no lidar samples in %.1fs on topic=%s (domain=%d). "
                    "Likely the firmware publishes under a different name — "
                    "check `ros2 topic list` on the dog and override via "
                    "LIDAR_TOPIC env (common alternatives: "
                    "rt/utlidar/cloud_undeskewed, rt/utlidar/livox_data).",
                    silence, LIDAR_TOPIC, self._domain,
                )
                self._last_no_data_warn_t = now

    def _handle(self, msg: _PointCloud2) -> None:
        # Topic-match heartbeat. A sample arrived — stamp this even if it
        # turns out to be unparsable, because the no-data warning is for
        # "wrong LIDAR_TOPIC" not "bad payload".
        self._last_sample_t = time.monotonic()
        self._last_no_data_warn_t = 0.0
        try:
            pts = _parse_pointcloud2(msg)
        except ValueError as exc:
            now = time.monotonic()
            if now - self._last_warn_t > 1.0:
                log.warning("pointcloud parse failed: %s", exc)
                self._last_warn_t = now
            return

        sectors = _scan_to_sectors(
            pts, self._n_sectors, SECTOR_DEG, RANGE_MAX_M,
        )
        scan_xy = _scan_for_browser(pts, SCAN_BROWSER_POINTS)

        # Find the closest non-clear sector. "Clear" sentinel = max range.
        below_clear = sectors < RANGE_MAX_M - 1e-3
        if np.any(below_clear):
            min_idx = int(np.argmin(np.where(below_clear, sectors, np.inf)))
            min_m = float(sectors[min_idx])
            # Sector i centered at -180 + (i+0.5)*sector_deg.
            min_bearing = -180.0 + (min_idx + 0.5) * SECTOR_DEG
        else:
            min_m = float(RANGE_MAX_M)
            min_bearing = 0.0

        snap = PerceptionSnapshot(
            stamp_ns=time.time_ns(),
            free_space=[round(float(d), 3) for d in sectors.tolist()],
            free_space_min_m=round(min_m, 3),
            free_space_min_bearing_deg=round(min_bearing, 1),
            sector_deg=SECTOR_DEG,
            scan_xy=[[round(float(x), 3), round(float(y), 3)]
                     for x, y in scan_xy.tolist()],
            n_raw_points=int(pts.shape[0]),
            have_data=True,
        )
        self._state.update(snap)

        if not self._got_first:
            self._got_first = True
            # z-range diagnostic helps verify LIDAR_HEIGHT_OFFSET_M.
            # In LIDAR frame: z is roughly centered around 0, range
            # ~[-floor_offset, +ceiling]. Floor returns sit near
            # z = -LIDAR_MOUNT_HEIGHT (~-0.40 m on Go2 EDU). If you see
            # z biased positive (~+0.40 m for floor), the cloud is
            # already in base_link → set LIDAR_HEIGHT_OFFSET_M=0.
            z_raw = pts[:, 2] if pts.size else np.zeros(1)
            log.info(
                "first lidar scan received: %d raw points, %d sectors "
                "with returns, %d points sent to browser, z_lidar range "
                "[%.2f, %.2f] m (offset=%.2f → floor-relative slab "
                "[%.2f, %.2f] m)",
                int(pts.shape[0]),
                int(np.sum(below_clear)),
                len(snap.scan_xy),
                float(z_raw.min()), float(z_raw.max()),
                LIDAR_HEIGHT_OFFSET_M,
                HEIGHT_MIN_M, HEIGHT_MAX_M,
            )
