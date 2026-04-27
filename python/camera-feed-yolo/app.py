#!/usr/bin/env python3
"""YOLOv8 webcam server: GStreamer (or OpenCV fallback) → YOLO → annotated MJPEG over WebSocket.

Env overrides: CAMERA_BACKEND=opencv|gstreamer, YOLO_MAX_FPS=<n>.
"""
from __future__ import annotations
import asyncio
import collections
import glob
import json
import logging
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
try:
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstApp", "1.0")
    from gi.repository import Gst, GLib
    _HAS_GSTREAMER = True
except Exception:
    Gst = None  # type: ignore
    GLib = None  # type: ignore
    _HAS_GSTREAMER = False
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from ultralytics import YOLO

_log_buffer = collections.deque(maxlen=200)
_last_v4l_target_log: tuple[str, tuple[str, ...]] | None = None
_last_camera_inventory_log: tuple[tuple[str, str], ...] | None = None
_last_no_camera_log = False


class _BufferHandler(logging.Handler):
    def emit(self, record):
        _log_buffer.append(self.format(record))


logging.basicConfig(level=logging.INFO, stream=sys.stdout)
_bh = _BufferHandler()
_bh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(_bh)
logger = logging.getLogger(__name__)

if _HAS_GSTREAMER:
    Gst.init(None)
    _glib_loop = GLib.MainLoop()
    threading.Thread(target=_glib_loop.run, daemon=True).start()


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    return None


def _has_cuda() -> bool:
    # wendy CLI bakes WENDY_HAS_GPU into the image from the agent's device
    # capability probe (see WENDY_HAS_GPU / WENDY_GPU_VENDOR build args). Prefer
    # that over runtime detection so CPU-only devices never load the CUDA path.
    hint = _env_bool("WENDY_HAS_GPU")
    if hint is False:
        return False
    if os.environ.get("WENDY_GPU_VENDOR", "").lower() not in ("", "nvidia"):
        return False
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        logger.warning("CUDA probe failed; falling back to CPU inference", exc_info=True)
        return False


def _is_rpi() -> bool:
    device_type = os.environ.get("WENDY_DEVICE_TYPE", "")
    if device_type.startswith("raspberrypi"):
        return True
    if device_type:
        return False
    try:
        return "Raspberry Pi" in Path("/proc/device-tree/model").read_text()
    except Exception:
        return False


_HAS_CUDA = _has_cuda()
IS_RPI = _is_rpi()

if not _HAS_CUDA:
    # Stops ONNX Runtime from probing CUDA providers on CPU-only devices.
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

_MAX_INFERENCE_FPS = float(os.environ.get("YOLO_MAX_FPS", "15" if _HAS_CUDA else "3"))
_MIN_INFERENCE_INTERVAL = 1.0 / _MAX_INFERENCE_FPS if _MAX_INFERENCE_FPS > 0 else 0
_INFERENCE_IMGSZ = 320 if _HAS_CUDA else 224
# RPi5 browns out under GStreamer + inference when USB-powered; OpenCV idles lighter.
_backend = os.environ.get("CAMERA_BACKEND", "auto").lower()
_FORCE_OPENCV = not _HAS_GSTREAMER or (_backend == "opencv") or (_backend == "auto" and (IS_RPI or not _HAS_CUDA))
logger.info("Platform: %s (device=%s, gpu=%s), CUDA: %s, capture: %s, max inference FPS: %s, imgsz: %s",
            os.environ.get("WENDY_PLATFORM", "rpi" if IS_RPI else "generic"),
            os.environ.get("WENDY_DEVICE_TYPE", "unknown"),
            os.environ.get("WENDY_GPU_VENDOR", "none"),
            _HAS_CUDA,
            "opencv" if _FORCE_OPENCV else "gstreamer", _MAX_INFERENCE_FPS, _INFERENCE_IMGSZ)

_model: YOLO | None = None
_model_lock = threading.Lock()


def _get_model() -> YOLO:
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        model_path = "yolov8n.onnx" if Path("yolov8n.onnx").exists() else "yolov8n.pt"
        logger.info("Loading YOLOv8n model (CUDA: %s)...", _HAS_CUDA)
        m = YOLO(model_path)
        logger.info("YOLOv8n ready — %d COCO classes, backend: %s", len(m.names), model_path)
        _model = m
        return _model

app = FastAPI()

_app_dir = Path(__file__).parent
_assets_dir = _app_dir / "assets"
if _assets_dir.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

IS_MACOS = platform.system() == "Darwin"
V4L_SYMLINK_DIRS = (Path("/dev/v4l/by-id"), Path("/dev/v4l/by-path"))


def _sysfs_video_node_path(path: str) -> Path:
    return Path("/sys/class/video4linux") / Path(path).name


def _v4l2_node_index(path: str) -> int:
    try:
        return int((_sysfs_video_node_path(path) / "index").read_text().strip())
    except Exception:
        return 999


def _v4l2_device_name(path: str) -> str:
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
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", "--device", path, "--all"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode()
        in_device_caps = False
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("Device Caps"):
                in_device_caps = True
                continue
            if not in_device_caps:
                continue
            if not line.startswith((" ", "\t")):
                break
            if stripped in {"Video Capture", "Video Capture Multiplanar"}:
                return True
        return False
    except Exception:
        return False


def _usb_device_id_for_video_node(path: str) -> str | None:
    try:
        device_path = (_sysfs_video_node_path(path) / "device").resolve()
    except Exception:
        return None

    for current in [device_path] + list(device_path.parents):
        if (current / "idVendor").exists() and (current / "idProduct").exists():
            return current.name.split(":", 1)[0]
    return None


def _linux_symlink_video_nodes() -> list[str]:
    nodes: list[str] = []

    def add(path: str):
        if path not in nodes:
            nodes.append(path)

    by_id = V4L_SYMLINK_DIRS[0]
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
        _log_v4l_targets_once("by-id", nodes)
        return nodes

    by_path = V4L_SYMLINK_DIRS[1]
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
    if nodes:
        _log_v4l_targets_once("by-path", nodes)
    return nodes


def _linux_candidate_video_nodes() -> list[str]:
    symlink_nodes = _linux_symlink_video_nodes()
    if symlink_nodes:
        return symlink_nodes

    nodes = sorted(glob.glob("/dev/video*"), key=lambda p: (_v4l2_node_index(p), p))
    if not nodes:
        _log_v4l_targets_once("none", [])
        return nodes

    usb_nodes = [path for path in nodes if _usb_device_id_for_video_node(path)]
    if usb_nodes:
        _log_v4l_targets_once("raw-usb", usb_nodes)
        return usb_nodes

    _log_v4l_targets_once("raw", nodes)
    return nodes


def _log_v4l_targets_once(source: str, nodes: list[str]):
    global _last_v4l_target_log

    current = (source, tuple(nodes))
    if current == _last_v4l_target_log:
        return
    _last_v4l_target_log = current

    if source == "by-id":
        logger.info("Stable V4L USB targets via /dev/v4l/by-id: %s", ", ".join(nodes))
    elif source == "by-path":
        logger.info("Stable V4L USB targets via /dev/v4l/by-path: %s", ", ".join(nodes))
    elif source == "raw-usb":
        logger.info("Falling back to raw USB V4L2 nodes: %s", ", ".join(nodes))
    elif source == "raw":
        logger.info("Falling back to raw V4L2 nodes: %s", ", ".join(nodes))
    else:
        logger.info("No raw V4L2 nodes available under /dev/video*")


def _log_camera_inventory_once(cameras: list[dict]):
    global _last_camera_inventory_log, _last_no_camera_log

    current = tuple((camera["id"], camera["name"]) for camera in cameras)
    if cameras:
        if current != _last_camera_inventory_log:
            logger.info(
                "Discovered %d camera(s) via V4L2: %s",
                len(cameras),
                ", ".join(f"{camera_id} ({name})" for camera_id, name in current),
            )
            _last_camera_inventory_log = current
        _last_no_camera_log = False
        return

    if not _last_no_camera_log:
        logger.info("No capture-classified V4L2 cameras found")
        _last_no_camera_log = True
    _last_camera_inventory_log = current


def _enumerate_linux_cameras() -> list[dict]:
    cameras = []
    for path in _linux_candidate_video_nodes():
        if _v4l2_is_capture(path):
            cameras.append({"id": path, "name": _v4l2_device_name(path)})
    _log_camera_inventory_once(cameras)
    return cameras


def enumerate_cameras() -> list[dict]:
    if not IS_MACOS or not _HAS_GSTREAMER:
        return _enumerate_linux_cameras()

    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Video/Source", Gst.Caps.from_string("video/x-raw"))
    monitor.start()
    devices = monitor.get_devices()

    cameras = []
    for i, dev in enumerate(devices):
        props = dev.get_properties()
        name = dev.get_display_name()
        idx = props.get_int("device.index")
        device_id = str(idx.value) if idx[0] else str(i)
        cameras.append({"id": device_id, "name": name})
    monitor.stop()
    return cameras


def build_source(device_id: str | None = None) -> str:
    if IS_MACOS:
        src = "avfvideosrc"
        if device_id is not None:
            src += f" device-index={device_id}"
    else:
        src = f"v4l2src device={device_id or '/dev/video0'}"
    return src


class YOLOCamera:
    """Camera frames stream at camera rate; inference runs in a background thread at
    its own (lower) rate and publishes box metadata that the client overlays as a canvas
    on top of the raw JPEG — so slow YOLO never stalls the stream."""

    def __init__(self):
        self.pipeline = None
        self.queues: dict[WebSocket, asyncio.Queue] = {}
        self._lock = threading.Lock()
        self._current_device: str | None = None
        self._loop = None
        self._bus = None
        self._bus_watch_id = None
        self._restart_task: asyncio.Task | None = None
        self._confidence = 0.25
        self._latest_raw: bytes | None = None
        self._raw_event = threading.Event()
        self._cached_meta: str = '{"detections":0,"inference_ms":0,"classes":{},"boxes":[],"frame_w":0,"frame_h":0}'
        self._last_meta: dict = {"detections": 0, "inference_ms": 0, "classes": {}, "boxes": [], "frame_w": 0, "frame_h": 0}
        self._capture_mode: str = "opencv" if _FORCE_OPENCV else "gstreamer"
        self._opencv_thread: threading.Thread | None = None
        self._inference_thread = threading.Thread(target=self._inference_loop, daemon=True)
        self._inference_thread.start()

    def _start_pipeline(self, device_id: str | None = None) -> Gst.Pipeline | None:
        src = build_source(device_id)
        appsink = "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
        if _HAS_CUDA:
            # Jetson has HW JPEG codecs, so decode+re-encode for quality control is cheap.
            pipelines = [
                f"{src} ! image/jpeg ! jpegdec ! jpegenc quality=85 ! {appsink}",
                f"{src} ! image/jpeg,width=640,height=480 ! jpegdec ! jpegenc quality=85 ! {appsink}",
                f"{src} ! video/x-raw ! videoconvert ! jpegenc quality=70 ! {appsink}",
                f"{src} ! videoconvert ! jpegenc quality=70 ! {appsink}",
                f"{src} ! image/jpeg ! {appsink}",
                f"{src} ! image/jpeg,width=640,height=480 ! {appsink}",
            ]
        else:
            # CPU: passthrough first — skip 30 fps decode/re-encode; inference decodes at its own rate.
            pipelines = [
                f"{src} ! image/jpeg ! {appsink}",
                f"{src} ! image/jpeg,width=640,height=480 ! {appsink}",
                f"{src} ! image/jpeg ! jpegdec ! jpegenc quality=70 ! {appsink}",
                f"{src} ! video/x-raw ! videoconvert ! jpegenc quality=70 ! {appsink}",
                f"{src} ! videoconvert ! jpegenc quality=70 ! {appsink}",
            ]
        # libcamerasrc covers RPi CSI modules; harmlessly fails parse on hosts without it.
        if not IS_MACOS:
            pipelines += [
                f"libcamerasrc ! video/x-raw,format=NV12,width=640,height=480 ! videoconvert ! jpegenc quality=70 ! {appsink}",
                f"libcamerasrc ! video/x-raw,width=640,height=480 ! videoconvert ! jpegenc quality=70 ! {appsink}",
            ]

        for p_str in pipelines:
            try:
                pipeline = Gst.parse_launch(p_str)
                ret = pipeline.set_state(Gst.State.PAUSED)
                if ret == Gst.StateChangeReturn.FAILURE:
                    pipeline.set_state(Gst.State.NULL)
                    logger.info("Pipeline failed: %s", p_str)
                    continue
                if ret == Gst.StateChangeReturn.ASYNC:
                    ret, _, _ = pipeline.get_state(5 * Gst.SECOND)
                    if ret == Gst.StateChangeReturn.FAILURE:
                        bus = pipeline.get_bus()
                        msg = bus.pop_filtered(Gst.MessageType.ERROR)
                        if msg:
                            err, debug = msg.parse_error()
                            logger.info(
                                "Pipeline preroll error for %s: %s%s",
                                p_str,
                                err,
                                f" ({debug})" if debug else "",
                            )
                        pipeline.set_state(Gst.State.NULL)
                        logger.info("Pipeline preroll failed: %s", p_str)
                        continue
                logger.info("Pipeline ready: %s", p_str)
                return pipeline
            except Exception as e:
                logger.info("Pipeline exception: %s — %s", p_str, e)
        return None

    def _candidate_devices(self, preferred_device: str | None = None) -> list[str]:
        candidates: list[str] = []
        enumerated_devices = [camera["id"] for camera in enumerate_cameras()]

        def add(device: str | None):
            if device and device not in candidates:
                candidates.append(device)

        if IS_MACOS:
            add(preferred_device)
            add(self._current_device)
        else:
            if preferred_device in enumerated_devices:
                add(preferred_device)
            if self._current_device in enumerated_devices:
                add(self._current_device)

        for device_id in enumerated_devices:
            add(device_id)
        return candidates

    def _start_any_pipeline(self, preferred_device: str | None = None) -> tuple[Gst.Pipeline | None, str | None]:
        for device_id in self._candidate_devices(preferred_device):
            pipeline = self._start_pipeline(device_id)
            if pipeline:
                return pipeline, device_id
        return None, preferred_device or self._current_device

    def _clear_pipeline_locked(self):
        if self._bus is not None:
            if self._bus_watch_id is not None:
                try:
                    self._bus.disconnect(self._bus_watch_id)
                except Exception:
                    pass
                self._bus_watch_id = None
            try:
                self._bus.remove_signal_watch()
            except Exception:
                pass
            self._bus = None

        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    def _attach_pipeline_locked(self, pipeline: Gst.Pipeline, device_id: str | None):
        self.pipeline = pipeline
        self._current_device = device_id

        sink = pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_new_sample)

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_watch_id = bus.connect("message", self._on_bus_message)
        self._bus = bus

        pipeline.set_state(Gst.State.PLAYING)
        logger.info("Camera streaming started on %s", device_id or "default device")

    def _ensure_restart_task(self, reason: str):
        if not self._loop:
            return
        if self._restart_task and not self._restart_task.done():
            return
        logger.info("Scheduling camera restart: %s", reason)
        self._restart_task = self._loop.create_task(self._restart_until_available(reason))

    def _request_restart(self, reason: str):
        if self._loop:
            self._loop.call_soon_threadsafe(self._ensure_restart_task, reason)

    def _on_bus_message(self, bus, message):
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.warning(
                "Camera pipeline error on %s: %s%s",
                self._current_device,
                err,
                f" ({debug})" if debug else "",
            )
        elif message.type == Gst.MessageType.EOS:
            logger.warning("Camera pipeline reached EOS on %s", self._current_device)
        else:
            return

        with self._lock:
            if bus != self._bus:
                return
            self._clear_pipeline_locked()

        self._request_restart("pipeline lost")

    async def _restart_until_available(self, reason: str):
        delay = 1.0
        try:
            while True:
                with self._lock:
                    if self.pipeline or not self.queues or self._capture_mode == "opencv":
                        return
                    preferred_device = self._current_device

                pipeline, resolved_device = await asyncio.to_thread(
                    self._start_any_pipeline,
                    preferred_device,
                )

                if pipeline:
                    with self._lock:
                        if self.pipeline or not self.queues:
                            pipeline.set_state(Gst.State.NULL)
                            return
                        self._attach_pipeline_locked(pipeline, resolved_device)
                    return

                if not IS_MACOS:
                    device = resolved_device or preferred_device
                    fell_back = await asyncio.to_thread(self._try_opencv_fallback, device)
                    if fell_back:
                        return

                logger.info("Camera unavailable after %s; retrying in %.1fs", reason, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 5.0)
        finally:
            with self._lock:
                if self._restart_task is asyncio.current_task():
                    self._restart_task = None

    def _distribute_frame(self, raw_jpeg: bytes):
        """Thread-safe: marshals the actual queue push onto the asyncio loop."""
        if not self._loop:
            return
        with self._lock:
            has_clients = bool(self.queues)
            meta = self._cached_meta
            queues = list(self.queues.values())

        if not has_clients:
            return

        def _do_push():
            for q in queues:
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    q.put_nowait((raw_jpeg, meta))
                except asyncio.QueueFull:
                    pass

        self._loop.call_soon_threadsafe(_do_push)

    def _on_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if not sample:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        data = bytes(mapinfo.data)
        buf.unmap(mapinfo)

        with self._lock:
            self._latest_raw = data
        self._raw_event.set()
        self._distribute_frame(data)
        return Gst.FlowReturn.OK

    def _try_opencv_fallback(self, device: str | None) -> bool:
        candidates = [device, "/dev/video0"] if device and device != "/dev/video0" else ["/dev/video0"]
        for dev in candidates:
            if dev is None:
                continue
            cap = cv2.VideoCapture(dev)
            opened = cap.isOpened()
            cap.release()
            if opened:
                logger.info("GStreamer unavailable; switching to OpenCV capture on %s", dev)
                with self._lock:
                    self._capture_mode = "opencv"
                    self._current_device = dev
                self._start_opencv_thread(dev)
                return True
        return False

    def _start_opencv_thread(self, device: str):
        if self._opencv_thread and self._opencv_thread.is_alive():
            return
        self._opencv_thread = threading.Thread(target=self._opencv_loop, args=(device,), daemon=True)
        self._opencv_thread.start()

    def _opencv_loop(self, initial_device: str):
        while True:
            with self._lock:
                device = self._current_device or initial_device

            cap = cv2.VideoCapture(device)
            if not cap.isOpened() and device != "/dev/video0":
                cap.release()
                cap = cv2.VideoCapture("/dev/video0")
            if not cap.isOpened():
                cap.release()
                cap = cv2.VideoCapture(0)

            if not cap.isOpened():
                cap.release()
                logger.warning("OpenCV: no camera on %s, retrying in 2s", device)
                time.sleep(2)
                continue

            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            logger.info("OpenCV camera opened: %s", device)

            try:
                while True:
                    with self._lock:
                        current_device = self._current_device or initial_device
                    if current_device != device:
                        logger.info("OpenCV: switching to %s", current_device)
                        break

                    with self._lock:
                        has_clients = bool(self.queues)
                    if not has_clients:
                        time.sleep(0.05)
                        continue

                    ret, frame = cap.read()
                    if not ret:
                        logger.warning("OpenCV: camera read failed on %s", device)
                        break

                    ok, jpeg_buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    if not ok:
                        continue

                    raw_jpeg = jpeg_buf.tobytes()
                    with self._lock:
                        self._latest_raw = raw_jpeg
                    self._raw_event.set()
                    if not _FORCE_OPENCV:
                        self._distribute_frame(raw_jpeg)
            finally:
                cap.release()

            logger.warning("OpenCV: camera lost on %s, retrying in 2s", device)
            time.sleep(2)

    def _inference_loop(self):
        last_inference = 0.0
        while True:
            self._raw_event.wait(timeout=1.0)
            self._raw_event.clear()

            now = time.monotonic()
            if now - last_inference < _MIN_INFERENCE_INTERVAL:
                continue

            with self._lock:
                raw = self._latest_raw
                self._latest_raw = None
                conf = self._confidence
                has_clients = bool(self.queues)

            if raw is None or not has_clients:
                continue

            model = _get_model()

            nparr = np.frombuffer(raw, np.uint8)
            frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if frame is None:
                continue

            h, w = frame.shape[:2]

            t0 = time.monotonic()
            results = model.predict(frame, conf=conf, imgsz=_INFERENCE_IMGSZ, verbose=False)
            inference_ms = (time.monotonic() - t0) * 1000
            last_inference = time.monotonic()

            boxes_out: list[dict] = []
            classes: dict[str, int] = {}
            for box in results[0].boxes:
                xyxy = box.xyxy[0].tolist() if hasattr(box.xyxy, 'tolist') else list(box.xyxy[0])
                cls_id = int(box.cls)
                c = float(box.conf)
                cls_name = model.names[cls_id]
                boxes_out.append({
                    "x1": float(xyxy[0]), "y1": float(xyxy[1]),
                    "x2": float(xyxy[2]), "y2": float(xyxy[3]),
                    "conf": c, "cls": cls_id, "name": cls_name,
                })
                classes[cls_name] = classes.get(cls_name, 0) + 1

            meta_dict = {
                "detections": len(boxes_out),
                "inference_ms": round(inference_ms, 1),
                "classes": classes,
                "boxes": boxes_out,
                "frame_w": w,
                "frame_h": h,
            }

            with self._lock:
                self._cached_meta = json.dumps(meta_dict)
                self._last_meta = meta_dict

            if _FORCE_OPENCV:
                self._distribute_frame(raw)

    async def add_client(self, ws: WebSocket) -> asyncio.Queue:
        self._loop = asyncio.get_running_loop()
        q = asyncio.Queue(maxsize=2)
        with self._lock:
            self.queues[ws] = q
            mode = self._capture_mode
            should_start_gst = mode == "gstreamer" and self.pipeline is None

        if mode == "opencv":
            device = self._current_device or "/dev/video0"
            self._start_opencv_thread(device)
        elif should_start_gst:
            self._ensure_restart_task("client connected")

        logger.info("Client added (total: %d)", len(self.queues))
        return q

    def remove_client(self, ws: WebSocket):
        with self._lock:
            self.queues.pop(ws, None)
            if not self.queues and self._capture_mode == "gstreamer":
                if self._restart_task and not self._restart_task.done():
                    self._restart_task.cancel()
                    self._restart_task = None
                self._clear_pipeline_locked()
                logger.info("Camera stopped (no clients)")
        logger.info("Client removed (total: %d)", len(self.queues))

    async def switch_camera(self, device_id: str):
        with self._lock:
            self._current_device = device_id
            if self._capture_mode == "gstreamer":
                self._clear_pipeline_locked()

        if self._capture_mode == "gstreamer":
            self._ensure_restart_task(f"switch to {device_id}")
        # OpenCV mode picks up _current_device change on its next iteration.
        logger.info("Requested camera switch to %s", device_id)

    def set_confidence(self, value: float):
        self._confidence = max(0.05, min(0.95, value))
        logger.info("Confidence threshold set to %.2f", self._confidence)


camera = YOLOCamera()

# Warm the model at startup so the first client connection isn't stalled by a
# multi-second GIL-holding torch load.
threading.Thread(target=_get_model, daemon=True).start()


@app.get("/cameras")
async def list_cameras():
    return JSONResponse(content=enumerate_cameras())


@app.websocket("/stream")
async def websocket_stream(websocket: WebSocket):
    await websocket.accept()
    try:
        q = await camera.add_client(websocket)
    except Exception as e:
        logger.error(f"Failed to start camera: {e}")
        await websocket.close(code=1011)
        return

    async def send_frames():
        try:
            while True:
                frame_data, meta = await q.get()
                await websocket.send_text(meta)
                await websocket.send_bytes(frame_data)
        except Exception:
            pass

    async def recv_commands():
        try:
            while True:
                msg = json.loads(await websocket.receive_text())
                if "switch_camera" in msg:
                    try:
                        await camera.switch_camera(msg["switch_camera"])
                    except Exception as e:
                        logger.error(f"Camera switch failed: {e}")
                if "confidence" in msg:
                    try:
                        camera.set_confidence(float(msg["confidence"]))
                    except (ValueError, TypeError):
                        pass
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    try:
        done, pending = await asyncio.wait(
            [asyncio.create_task(send_frames()), asyncio.create_task(recv_commands())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        camera.remove_client(websocket)


@app.get("/logs")
async def get_logs():
    return JSONResponse(content=list(_log_buffer))


@app.get("/debug")
async def debug_info():
    cameras = enumerate_cameras()
    model = _model
    return JSONResponse(
        content={
            "mode": "yolo-mjpeg-ws",
            "model": "yolov8n",
            "cuda": _HAS_CUDA,
            "is_rpi": IS_RPI,
            "max_inference_fps": _MAX_INFERENCE_FPS,
            "inference_imgsz": _INFERENCE_IMGSZ,
            "capture_backend": camera._capture_mode,
            "device": str(model.device) if model else "not loaded",
            "confidence": camera._confidence,
            "cameras": cameras,
            "pipeline_state": camera.pipeline.get_state(0)[1].value_nick if camera.pipeline else None,
            "num_clients": len(camera.queues),
            "last_detections": camera._last_meta,
        }
    )


@app.get("/")
async def root():
    return FileResponse(Path(__file__).parent / "index.html", media_type="text/html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port={{.PORT}})
