"""YOLO detection module — runs inference loop, VLM workers, hardware stats."""

import os
import platform
import sys
import time
import threading
import queue as _queue
import uuid
import logging
import logging.handlers
import glob
import base64
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from collections import deque

# Block ONNX Runtime entirely — it crashes on Jetson before Python can init it.
if os.environ.get("DISABLE_ONNXRUNTIME", "1").lower() in ("1", "true", "yes"):
    sys.modules["onnxruntime"] = None

os.environ.setdefault("YOLO_VERBOSE", "False")
os.environ.setdefault("CUDA_MODULE_LOADING", "LAZY")
# Silence OpenCV's V4L2/OBSensor backend probe warnings. cv2.VideoCapture(path)
# tries multiple backends internally and emits a WARN per failed probe even when
# the call ultimately succeeds — confused users into thinking the camera failed.
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
os.environ.setdefault("OPENCV_VIDEOIO_DEBUG", "0")
# OpenCV's OBSensor (Orbbec) backend probes camera indices on every VideoCapture
# open and logs at ERROR severity per miss — not silenced by OPENCV_LOG_LEVEL=ERROR.
# We don't use Orbbec cameras here; deprioritise the backend so it never probes.
os.environ.setdefault("OPENCV_VIDEOIO_PRIORITY_OBSENSOR", "0")

# Persistent logging — /logs on Jetson, local fallback for dev
_LOG_DIR = Path(os.environ.get("LOG_DIR", "/logs"))
try:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    _LOG_DIR = Path(__file__).parent / "logs"
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("detector")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_fh = logging.handlers.RotatingFileHandler(_LOG_DIR / "detector.log", maxBytes=10 * 1024 * 1024, backupCount=5)
_fh.setFormatter(_fmt)
logger.addHandler(_fh)
# NOTE: No StreamHandler — writing to stderr (a pipe to the container log driver)
# causes pipe_write to block when the pipe buffer fills. Since Python's logging
# module holds an internal lock during write, this deadlocks the event loop when
# the inference thread floods the pipe with DETECTION lines.
# All logs go to /logs/detector.log only. Use `cat /logs/detector.log` to read.

# Optional imports — detection is disabled gracefully if not installed
try:
    import numpy as np
    import psutil
    import cv2
    import torch
    from ultralytics import YOLO
    # Belt-and-braces: env var alone is sometimes ignored when cv2 is loaded
    # through ultralytics' transitive import. Reassert at runtime.
    cv2.setLogLevel(2)  # 2 = LOG_LEVEL_ERROR
    DETECTION_AVAILABLE = True
except ImportError as _e:
    logger.warning(f"Detection unavailable (missing deps: {_e}). YOLO disabled.")
    DETECTION_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────

MODEL_NAME = os.environ.get("YOLO_MODEL", "yolov8n.pt")
MODELS_DIR = Path(os.environ.get("YOLO_MODELS_DIR", "/yolo-models"))
ENGINE_NAME = os.environ.get("YOLO_ENGINE", "")
EXPORT_TRT = os.environ.get("YOLO_EXPORT_TRT", "0").lower() in ("1", "true", "yes")
_device_env = os.environ.get("YOLO_DEVICE", "0")
# Auto-fallback to CPU if CUDA not available (e.g. macOS dev)
def _resolve_device(d: str) -> str:
    try:
        import torch as _t
        if not _t.cuda.is_available() and d.isdigit():
            return "cpu"
    except Exception:
        pass
    return d
DEVICE = _resolve_device(_device_env)
IMG_SIZE = int(os.environ.get("YOLO_IMGSZ", "640"))
HALF = os.environ.get("YOLO_HALF", "1").lower() in ("1", "true", "yes") and DEVICE != "cpu"
CONF = float(os.environ.get("YOLO_CONF", "0.5"))
WARMUP_ITERS = int(os.environ.get("YOLO_WARMUP", "3"))
DRAW_OVERLAY = os.environ.get("DRAW_OVERLAY", "1").lower() in ("1", "true", "yes")
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "80"))
TARGET_FPS = float(os.environ.get("TARGET_FPS", "15"))

USE_GSTREAMER = os.environ.get("USE_GSTREAMER", "1").lower() in ("1", "true", "yes")
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
CAMERA_WIDTH = int(os.environ.get("CAMERA_WIDTH", "1280"))
CAMERA_HEIGHT = int(os.environ.get("CAMERA_HEIGHT", "720"))
CAMERA_FPS = int(os.environ.get("CAMERA_FPS", "30"))
CAMERA_SOURCE = os.environ.get("CAMERA_SOURCE", "").lower()
CAMERA_PIPELINE = os.environ.get("CAMERA_GSTREAMER_PIPELINE", "")

VLM_ENABLED = os.environ.get("VLM_ENABLED", "1").lower() in ("1", "true", "yes")
VLM_URL = os.environ.get("VLM_URL", "http://localhost:8090")
VLM_CONF_THRESHOLD = float(os.environ.get("VLM_CONF_THRESHOLD", "0.7"))
VLM_QUESTION = os.environ.get("VLM_QUESTION", "")
VLM_TIMEOUT = int(os.environ.get("VLM_TIMEOUT", "30"))

LOG_SIZE = int(os.environ.get("YOLO_LOG_SIZE", "100"))

# ── State ─────────────────────────────────────────────────────────────────────

detection_log: deque = deque(maxlen=LOG_SIZE)
detection_lock = threading.Lock()

vlm_answers_log: deque = deque(maxlen=200)
vlm_answers_lock = threading.Lock()

CROP_LOG_SIZE = int(os.environ.get("YOLO_CROP_LOG_SIZE", "50"))
crop_conf_threshold = float(os.environ.get("YOLO_CROP_CONF", "0.7"))
crop_conf_lock = threading.Lock()
crop_log: deque = deque(maxlen=CROP_LOG_SIZE)
# Maps crop_id → full frame JPEG bytes (kept for the last N crops)
crop_frames: dict[str, bytes] = {}
CROP_FRAMES_MAX = CROP_LOG_SIZE

latest_frame_bytes = None
latest_frame_id = 0
frame_condition = threading.Condition()

stop_event = threading.Event()
pause_event = threading.Event()
inference_thread = None
model_switch_lock = threading.Lock()
active_model_name = MODEL_NAME

video_fps = 0.0
inference_fps = 0.0
fps_lock = threading.Lock()

_vlm_queue: _queue.Queue = _queue.Queue(maxsize=5)
vlm_questions: list[dict] = []
vlm_questions_lock = threading.Lock()

class_stats: dict = {}
class_stats_lock = threading.Lock()

vlm_interval = 0.0
vlm_interval_lock = threading.Lock()
vlm_interval_changed = threading.Event()

vlm_conf_threshold = VLM_CONF_THRESHOLD
vlm_conf_lock = threading.Lock()

vlm_classes: set[str] = set()  # empty = all classes, non-empty = only these classes
vlm_classes_lock = threading.Lock()

vlm_connected = False
vlm_last_error: str | None = None
vlm_last_check: float = 0
vlm_status_lock = threading.Lock()
_VLM_CHECK_INTERVAL = 10.0  # seconds between health checks when disconnected
_vlm_call_lock = threading.Lock()  # only one VLM request at a time
_vlm_last_detection_call: float = 0  # rate limit detection-triggered VLM calls
_VLM_MIN_DETECTION_INTERVAL = 3.0  # min seconds between detection-triggered VLM calls

# VLM auto-disable after consecutive failures
_vlm_consecutive_failures = 0
_VLM_MAX_CONSECUTIVE_FAILURES = 3  # disable after this many failures
_vlm_last_reconnect_attempt: float = 0
_VLM_RECONNECT_INTERVAL = 30.0  # try reconnecting every 30s

# Spatial cache — avoid re-describing the same object at the same location
_vlm_spatial_cache: dict[str, float] = {}  # "{class}_{grid_x}_{grid_y}" → timestamp
_VLM_SPATIAL_CACHE_TTL = 300.0  # 5 minutes
_vlm_spatial_cache_lock = threading.Lock()

# Conditional crop encoding — only encode if someone is reading
_last_crop_read: float = 0  # updated when /api/crops is called

trt_export_status: dict = {}

model = None
_active_cap = None
_active_cap_lock = threading.Lock()

software_zoom = 1.0  # 1.0 = no zoom, 2.0 = 2x zoom (center crop)
software_pan = 0.0   # -1.0 = full left, 0.0 = center, 1.0 = full right
software_tilt = 0.0  # -1.0 = full up, 0.0 = center, 1.0 = full down
software_zoom_lock = threading.Lock()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _set_latest_frame(frame_bytes: bytes) -> None:
    global latest_frame_bytes, latest_frame_id
    with frame_condition:
        latest_frame_bytes = frame_bytes
        latest_frame_id += 1
        frame_condition.notify_all()


def _make_status_frame(message: str, detail: str | None = None):
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.putText(frame, message, (40, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 200, 255), 2, cv2.LINE_AA)
    if detail:
        cv2.putText(frame, detail, (40, 160), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (200, 200, 200), 2, cv2.LINE_AA)
    return frame


def _apply_software_zoom(frame):
    """Crop frame based on software zoom/pan/tilt and resize back to original dimensions."""
    with software_zoom_lock:
        zoom = software_zoom
        pan = software_pan
        tilt = software_tilt
    if zoom <= 1.0:
        return frame
    h, w = frame.shape[:2]
    crop_w = int(w / zoom)
    crop_h = int(h / zoom)
    # Center + pan/tilt offset
    cx = w // 2 + int(pan * (w - crop_w) // 2)
    cy = h // 2 + int(tilt * (h - crop_h) // 2)
    # Clamp to frame bounds
    x1 = max(0, min(w - crop_w, cx - crop_w // 2))
    y1 = max(0, min(h - crop_h, cy - crop_h // 2))
    cropped = frame[y1:y1 + crop_h, x1:x1 + crop_w]
    return cv2.resize(cropped, (w, h), interpolation=cv2.INTER_LINEAR)


def _encode_frame(frame) -> bytes:
    _, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return buffer.tobytes()


def _build_csi_pipeline() -> str:
    return (
        "nvarguscamerasrc ! "
        f"video/x-raw(memory:NVMM), width=(int){CAMERA_WIDTH}, height=(int){CAMERA_HEIGHT}, "
        f"framerate=(fraction){CAMERA_FPS}/1, format=(string)NV12 ! "
        "nvvidconv ! video/x-raw, format=(string)BGRx ! "
        "videoconvert ! video/x-raw, format=(string)BGR ! "
        "appsink drop=1 sync=0"
    )


def _build_v4l2_pipeline(device_path: str) -> str:
    """Default V4L2 pipeline: raw video at configured caps -> BGR appsink."""
    return (
        f"v4l2src device={device_path} ! "
        f"video/x-raw, width=(int){CAMERA_WIDTH}, height=(int){CAMERA_HEIGHT}, "
        f"framerate=(fraction){CAMERA_FPS}/1 ! "
        "videoconvert ! video/x-raw, format=(string)BGR ! "
        "appsink drop=1 sync=0"
    )


def _v4l2_pipeline_matrix(device_path: str) -> list[str]:
    """Pipelines to try per device, ordered MJPEG-first.

    Most modern UVC webcams emit MJPEG at higher framerates than raw YUYV,
    so jpegdec is faster and works on cameras that won't negotiate raw caps.
    Raw variants are kept as later attempts for cameras that emit YUYV.
    """
    src = f"v4l2src device={device_path}"
    return [
        f"{src} ! image/jpeg ! jpegdec ! videoconvert ! "
        f"video/x-raw, format=(string)BGR ! appsink drop=1 sync=0",
        f"{src} ! image/jpeg, width=(int){CAMERA_WIDTH}, height=(int){CAMERA_HEIGHT}, "
        f"framerate=(fraction){CAMERA_FPS}/1 ! jpegdec ! videoconvert ! "
        f"video/x-raw, format=(string)BGR ! appsink drop=1 sync=0",
        _build_v4l2_pipeline(device_path),
        f"{src} ! videoconvert ! video/x-raw, format=(string)BGR ! "
        f"appsink drop=1 sync=0",
    ]


def _try_gstreamer(pipeline: str):
    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
    if cap.isOpened():
        return cap
    cap.release()
    return None


def _try_opencv(device):
    """device may be a /dev/videoN path string or an int index."""
    # Pin V4L2 on Linux to skip OpenCV's multi-backend auto-probe (which logs
    # `obsensor_uvc_stream_channel.cpp:158 ... Camera index out of range` at
    # ERROR for every miss). On macOS dev, fall back to auto so AVFoundation
    # still works.
    if sys.platform.startswith("linux") and hasattr(cv2, "CAP_V4L2"):
        cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    else:
        cap = cv2.VideoCapture(device)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, CAMERA_FPS)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if cap.isOpened():
        return cap
    cap.release()
    return None


def _open_capture():
    """Open the first camera that yields a working capture.

    Order:
      1. CAMERA_GSTREAMER_PIPELINE (explicit user override).
      2. Configured CAMERA_SOURCE at CAMERA_INDEX (csi / v4l2 / both).
      3. Discovered capture-capable V4L2 nodes — iterate the pipeline
         matrix, then raw OpenCV. This is what survives a camera landing
         on /dev/videoN where N != configured CAMERA_INDEX.
      4. cv2.VideoCapture(CAMERA_INDEX) as a final hatch.
    """
    configured_path = f"/dev/video{CAMERA_INDEX}"

    # 1. User-explicit literal pipeline.
    if USE_GSTREAMER and CAMERA_PIPELINE:
        cap = _try_gstreamer(CAMERA_PIPELINE)
        if cap is not None:
            return cap, f"gstreamer:{CAMERA_PIPELINE}"

    # 2. Configured source at configured index.
    if USE_GSTREAMER and not CAMERA_PIPELINE:
        if CAMERA_SOURCE == "csi":
            cap = _try_gstreamer(_build_csi_pipeline())
            if cap is not None:
                return cap, "gstreamer:csi"
        elif CAMERA_SOURCE == "v4l2":
            for p in _v4l2_pipeline_matrix(configured_path):
                cap = _try_gstreamer(p)
                if cap is not None:
                    return cap, f"gstreamer:{p}"
        else:
            cap = _try_gstreamer(_build_csi_pipeline())
            if cap is not None:
                return cap, "gstreamer:csi"
            for p in _v4l2_pipeline_matrix(configured_path):
                cap = _try_gstreamer(p)
                if cap is not None:
                    return cap, f"gstreamer:{p}"

    # 3. Discovery fallthrough.
    try:
        from cameras import discover_capture_nodes  # lazy: avoids module-load order issues
        discovered = discover_capture_nodes()
    except Exception as exc:
        logger.warning(f"Camera discovery failed: {exc}")
        discovered = []

    if discovered:
        logger.info(
            "Discovered capture nodes: "
            + ", ".join(f"{n['path']} ({n['name']})" for n in discovered)
        )
        for node in discovered:
            path = node["path"]
            if USE_GSTREAMER:
                for p in _v4l2_pipeline_matrix(path):
                    cap = _try_gstreamer(p)
                    if cap is not None:
                        logger.info(f"Camera opened via GStreamer on {path}")
                        return cap, f"gstreamer:{p}"
            cap = _try_opencv(path)
            if cap is not None:
                logger.info(f"Camera opened via OpenCV on {path}")
                return cap, f"opencv:{path}"
    else:
        logger.warning("No capture-classified V4L2 nodes found via discovery")

    # 4. Final hatch: integer-index OpenCV.
    cap = _try_opencv(CAMERA_INDEX)
    if cap is not None:
        return cap, f"opencv:{configured_path}"

    return None, "none"


def _resolve_engine_path():
    if ENGINE_NAME:
        p = Path(ENGINE_NAME)
        return p if p.is_absolute() else MODELS_DIR / p
    p = Path(MODEL_NAME)
    if p.suffix == ".engine":
        return p if p.is_absolute() else MODELS_DIR / p
    return MODELS_DIR / f"{p.stem}.engine"


def _copy_if_missing(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists() and src.exists():
        import shutil
        shutil.copy(src, dst)


def _export_engine(m, engine_path: Path):
    logger.info("Exporting TensorRT engine (this can take a while)...")
    try:
        # workspace=0.25 (was 1) — Orin Nano shares 8GB unified memory with VLM /
        # base PyTorch / active YOLO; a 1GB workspace forces TRT to skip many
        # tactics with "insufficient memory" warnings, producing a slower engine.
        # 256MB fits the useful tactics for yolov8n-class models.
        result = m.export(format="engine", device=DEVICE, imgsz=IMG_SIZE, half=HALF, workspace=0.25)
    except Exception as exc:
        logger.error(f"TensorRT export failed: {exc}")
        return None
    export_path = None
    if isinstance(result, (str, Path)):
        export_path = Path(str(result))
    elif isinstance(result, dict):
        for key in ("file", "path", "engine"):
            if key in result:
                export_path = Path(str(result[key]))
                break
    if export_path is None or not export_path.exists():
        stem = Path(MODEL_NAME).stem
        candidates = list(Path("runs").rglob(f"{stem}*.engine")) or list(Path(".").rglob("*.engine"))
        if candidates:
            export_path = max(candidates, key=lambda p: p.stat().st_mtime)
    if export_path and export_path.exists():
        _copy_if_missing(export_path, engine_path)
        return engine_path
    return None


def _load_model():
    global model, active_model_name, MODELS_DIR
    try:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        MODELS_DIR = Path(__file__).parent / "yolo-models"
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
    engine_path = _resolve_engine_path()
    if engine_path and engine_path.exists():
        logger.info(f"Loading TensorRT engine: {engine_path}")
        return YOLO(str(engine_path))
    model_path = Path(MODEL_NAME)
    if not model_path.is_absolute():
        model_path = MODELS_DIR / model_path
    if model_path.exists():
        logger.info(f"Loading model: {model_path}")
        m = YOLO(str(model_path))
    else:
        logger.info(f"Downloading {MODEL_NAME}...")
        m = YOLO(MODEL_NAME)
        cache = Path.home() / ".cache" / "ultralytics" / MODEL_NAME
        _copy_if_missing(cache, model_path)
    if EXPORT_TRT and engine_path:
        exported = _export_engine(m, engine_path)
        if exported and exported.exists():
            logger.info(f"Loading exported TRT engine: {exported}")
            return YOLO(str(exported))
    return m


# Directories searched for .pt model files. Path(__file__).parent is /app/backend
# at runtime (where the bundled .pt files actually live, per Dockerfile WORKDIR);
# /app and MODELS_DIR are kept for backward compatibility with templates that
# stage models elsewhere.
_MODEL_SEARCH_DIRS: tuple[Path, ...] = (
    Path(__file__).parent,
    Path("/app"),
    MODELS_DIR,
)


def _list_available_models() -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for directory in _MODEL_SEARCH_DIRS:
        if not directory.exists():
            continue
        for f in sorted(directory.glob("*.pt")):
            s = str(f)
            if s not in seen:
                seen.add(s)
                found.append(s)
    return found


def _warmup_predict(m) -> None:
    """One synchronous predict() on a black frame. Cheap (a few hundred ms),
    forces CUDA kernel init + JIT compilation + autograd graph build, so the
    first user-visible predict() after a hot-swap is fast instead of slow."""
    try:
        dummy = np.zeros((IMG_SIZE, IMG_SIZE, 3), dtype=np.uint8)
        with torch.inference_mode():
            m.predict(source=dummy, device=DEVICE, imgsz=IMG_SIZE, half=HALF, conf=CONF, verbose=False)
    except Exception as exc:
        logger.warning(f"Warmup predict failed (continuing): {exc}")


def _hot_swap_model(new_model, model_path: str, only_if_active: str | None = None) -> bool:
    """Atomically replace the active model. The inference loop reads `model`
    once per iteration and picks up the new reference on the next frame —
    no thread restart, no camera re-open, no UI freeze.

    Caller must have already loaded + warmed the new_model so first-frame
    latency on the new model is comparable to steady-state.

    If only_if_active is set, the swap is skipped when active_model_name has
    drifted off it (e.g. a concurrent switch_to_model raced ahead). Used by
    background TRT export so a late engine doesn't clobber a newer choice.
    Returns True when the swap landed."""
    global model, active_model_name
    with model_switch_lock:
        if only_if_active is not None and active_model_name != only_if_active:
            return False
        model = new_model
        active_model_name = model_path
    _init_class_stats()
    logger.info(f"Hot-swapped active model -> {model_path}")
    return True


def _background_trt_export(model_path: str, engine_path: Path) -> None:
    """Build a TRT engine for `model_path` in the background. If this model is
    currently active when the export completes, hot-swap to the engine so
    inference picks up the (much faster) engine without restarting."""
    global trt_export_status
    trt_export_status[model_path] = "exporting"
    try:
        tmp = YOLO(model_path)
        exported = _export_engine(tmp, engine_path)
        if exported and exported.exists():
            trt_export_status[model_path] = "done"
            logger.info(f"Background TRT export done: {engine_path}")
            if active_model_name == model_path:
                engine_model = YOLO(str(engine_path))
                try:
                    engine_model.fuse()
                except Exception:
                    pass
                _warmup_predict(engine_model)
                _hot_swap_model(engine_model, model_path, only_if_active=model_path)
        else:
            trt_export_status[model_path] = "failed"
    except Exception as e:
        trt_export_status[model_path] = "failed"
        logger.error(f"Background TRT export failed: {e}")


def _prewarm_engines() -> None:
    """Pre-export TRT engines for every .pt under MODELS_DIR that doesn't
    have one yet. Runs in a background thread at startup so the first
    profile switch finds a cached engine instead of triggering a 30-60s
    .pt-based slowdown + GPU-contention while a foreground export runs.

    Sequential (one export at a time) to avoid GPU memory contention."""
    if not EXPORT_TRT:
        return
    candidates: list[tuple[str, Path]] = []
    seen_stems: set[str] = set()
    for directory in _MODEL_SEARCH_DIRS:
        if not directory.exists():
            continue
        for pt in sorted(directory.glob("*.pt")):
            if pt.stem in seen_stems:
                continue
            seen_stems.add(pt.stem)
            engine = MODELS_DIR / f"{pt.stem}.engine"
            if engine.exists():
                continue
            if str(pt) in trt_export_status:
                continue
            candidates.append((str(pt), engine))
    if not candidates:
        return
    logger.info(f"Pre-exporting TRT engines for {len(candidates)} model(s): "
                + ", ".join(p for p, _ in candidates))
    for pt_path, engine_path in candidates:
        _background_trt_export(pt_path, engine_path)


def switch_to_model(model_path: str) -> dict:
    """Switch the active model with no inference-thread restart and no
    camera re-open. The inference loop reads `model` per-iteration; we
    load + warm up the new model first, then atomically swap the
    reference. The old model is GC'd on next iteration."""
    stem = Path(model_path).stem
    engine_path = MODELS_DIR / f"{stem}.engine"
    if engine_path.exists():
        new_model = YOLO(str(engine_path))
        loaded_as = "engine"
    else:
        new_model = YOLO(model_path)
        loaded_as = "pt"
        if EXPORT_TRT and model_path not in trt_export_status:
            threading.Thread(
                target=_background_trt_export,
                args=(model_path, engine_path),
                daemon=True,
            ).start()
    try:
        new_model.fuse()
    except Exception:
        pass
    _warmup_predict(new_model)
    _hot_swap_model(new_model, model_path)
    return {"model": model_path, "loaded_as": loaded_as}


# ── Class stats ───────────────────────────────────────────────────────────────

def _init_class_stats() -> None:
    global class_stats
    names = model.names
    cls_list = list(names.values()) if isinstance(names, dict) else list(names)
    with class_stats_lock:
        class_stats = {name: {"count": 0, "last_seen": None, "visible_since": None} for name in cls_list}


def _update_class_visibility(results) -> None:
    now = datetime.now(timezone.utc)
    detected: set = set()
    for result in results:
        for box in result.boxes:
            if float(box.conf[0]) >= CONF:
                cls_id = int(box.cls[0])
                names = model.names
                cls_name = names.get(cls_id, str(cls_id)) if isinstance(names, dict) else names[cls_id]
                detected.add(cls_name)
    with class_stats_lock:
        for cls_name, stats in class_stats.items():
            if cls_name in detected:
                if stats["visible_since"] is None:
                    stats["visible_since"] = now
            else:
                stats["visible_since"] = None


# ── VLM ──────────────────────────────────────────────────────────────────────

def _check_vlm_health() -> bool:
    """Quick health check — updates vlm_connected state."""
    global vlm_connected, vlm_last_error, vlm_last_check
    import requests as _requests
    try:
        resp = _requests.get(f"{VLM_URL}/health", timeout=3)
        with vlm_status_lock:
            vlm_connected = resp.status_code == 200
            vlm_last_error = None
            vlm_last_check = time.monotonic()
        return vlm_connected
    except Exception as exc:
        with vlm_status_lock:
            vlm_connected = False
            vlm_last_error = str(exc)
            vlm_last_check = time.monotonic()
        return False


def _is_vlm_available() -> bool:
    """Check if VLM is available, with rate-limited health checks."""
    with vlm_status_lock:
        if vlm_connected:
            return True
        if time.monotonic() - vlm_last_check < _VLM_CHECK_INTERVAL:
            return False
    return _check_vlm_health()


class _VlmUnavailable(Exception):
    """Raised when VLM service is not reachable — suppresses log spam."""
    pass


_VLM_MAX_IMAGE_BYTES = 20000  # resize images larger than this before sending


def _resize_for_vlm(image_bytes: bytes) -> bytes:
    """Resize large images to reduce VLM processing time and memory."""
    if len(image_bytes) <= _VLM_MAX_IMAGE_BYTES:
        return image_bytes
    try:
        import numpy as np
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return image_bytes
        h, w = frame.shape[:2]
        max_dim = 672  # match VLM's internal MAX_IMAGE_SIZE
        if max(h, w) > max_dim:
            scale = max_dim / max(h, w)
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        resized = buf.tobytes()
        logger.info(f"VLM image resized: {len(image_bytes)} -> {len(resized)} bytes")
        return resized
    except Exception:
        return image_bytes


def _call_vlm(image_bytes: bytes, question: str) -> str:
    global vlm_connected, vlm_last_error, _vlm_consecutive_failures
    import requests as _requests

    if not _is_vlm_available():
        raise _VlmUnavailable()

    # Auto-disabled after too many consecutive failures
    if _vlm_consecutive_failures >= _VLM_MAX_CONSECUTIVE_FAILURES:
        raise _VlmUnavailable()

    # Resize large images before sending
    image_bytes = _resize_for_vlm(image_bytes)

    # Serialize VLM calls — only one at a time
    with _vlm_call_lock:
        image_b64 = base64.b64encode(image_bytes).decode()
        logger.info(f"VLM request starting: {len(image_bytes)} bytes, Q: {question[:50]}")
        vlm_start = time.monotonic()
        try:
            resp = _requests.post(
                f"{VLM_URL}/question",
                json={"image": image_b64, "question": question},
                timeout=min(VLM_TIMEOUT, 30),  # cap at 30s — if VLM can't respond, skip
            )
            vlm_elapsed = time.monotonic() - vlm_start
            logger.info(f"VLM response in {vlm_elapsed:.1f}s, status={resp.status_code}")
            with vlm_status_lock:
                vlm_connected = True
                vlm_last_error = None
            _vlm_consecutive_failures = 0  # reset on success
            resp.raise_for_status()
            data = resp.json()
            return (data.get("answer") or data.get("response") or data.get("text") or str(data)).strip()
        except Exception as exc:
            vlm_elapsed = time.monotonic() - vlm_start
            _vlm_consecutive_failures += 1
            if _vlm_consecutive_failures >= _VLM_MAX_CONSECUTIVE_FAILURES:
                logger.warning(f"VLM auto-disabled after {_vlm_consecutive_failures} consecutive failures")
            else:
                logger.error(f"VLM request failed after {vlm_elapsed:.1f}s ({_vlm_consecutive_failures}/{_VLM_MAX_CONSECUTIVE_FAILURES}): {exc}")
            with vlm_status_lock:
                vlm_connected = False
                vlm_last_error = str(exc)
            raise


def _vlm_try_reconnect() -> None:
    """Try to reconnect to VLM if auto-disabled. Called from worker idle loop."""
    global _vlm_consecutive_failures, _vlm_last_reconnect_attempt
    if _vlm_consecutive_failures < _VLM_MAX_CONSECUTIVE_FAILURES:
        return
    now = time.monotonic()
    if now - _vlm_last_reconnect_attempt < _VLM_RECONNECT_INTERVAL:
        return
    _vlm_last_reconnect_attempt = now
    if _check_vlm_health():
        _vlm_consecutive_failures = 0
        logger.info("VLM reconnected — re-enabling after previous failures")


def _vlm_cleanup_spatial_cache() -> None:
    """Remove expired spatial cache entries."""
    now = time.monotonic()
    with _vlm_spatial_cache_lock:
        expired = [k for k, t in _vlm_spatial_cache.items() if now - t >= _VLM_SPATIAL_CACHE_TTL]
        for k in expired:
            del _vlm_spatial_cache[k]


def _vlm_check_spatial_cache(cls_name: str, bbox: list[int]) -> bool:
    """Return True if this detection was already described at this location recently."""
    grid_x = bbox[0] // 100
    grid_y = bbox[1] // 100
    key = f"{cls_name}_{grid_x}_{grid_y}"
    now = time.monotonic()
    with _vlm_spatial_cache_lock:
        if key in _vlm_spatial_cache:
            if now - _vlm_spatial_cache[key] < _VLM_SPATIAL_CACHE_TTL:
                return True
            del _vlm_spatial_cache[key]
    return False


def _vlm_mark_spatial_cache(cls_name: str, bbox: list[int]) -> None:
    """Mark this detection location as recently described."""
    grid_x = bbox[0] // 100
    grid_y = bbox[1] // 100
    key = f"{cls_name}_{grid_x}_{grid_y}"
    with _vlm_spatial_cache_lock:
        _vlm_spatial_cache[key] = time.monotonic()


def _vlm_worker() -> None:
    cleanup_counter = 0
    while True:
        try:
            entry, crop_bytes, questions = _vlm_queue.get(timeout=1.0)
        except _queue.Empty:
            _vlm_try_reconnect()
            cleanup_counter += 1
            if cleanup_counter >= 60:  # cleanup every ~60s
                _vlm_cleanup_spatial_cache()
                cleanup_counter = 0
            continue
        if not _is_vlm_available() or _vlm_consecutive_failures >= _VLM_MAX_CONSECUTIVE_FAILURES:
            _vlm_queue.task_done()
            continue
        answers = []
        for q in questions:
            try:
                answer = _call_vlm(crop_bytes, q)
                answers.append({"question": q, "answer": answer})
                logger.info(f"VLM Q: {q} | A: {answer}")
            except _VlmUnavailable:
                break  # stop trying remaining questions
            except Exception as exc:
                logger.error(f"VLM worker error: {exc}")
        entry["vlm_answers"] = answers
        if answers:
            with vlm_answers_lock:
                vlm_answers_log.append({
                    "label": entry.get("label", ""),
                    "confidence": entry.get("confidence", 0),
                    "timestamp": entry.get("timestamp", ""),
                    "vlm_answers": list(answers),
                })
        _vlm_queue.task_done()


def _vlm_periodic_thread() -> None:
    while True:
        with vlm_interval_lock:
            interval = vlm_interval
        if interval <= 0:
            vlm_interval_changed.wait(timeout=1.0)
            vlm_interval_changed.clear()
            continue
        vlm_interval_changed.wait(timeout=interval)
        vlm_interval_changed.clear()
        with vlm_interval_lock:
            interval = vlm_interval
        if interval <= 0:
            continue
        frame_bytes = latest_frame_bytes
        if frame_bytes is None:
            continue
        with vlm_questions_lock:
            questions_snapshot = [q["question"] for q in vlm_questions]
        if not questions_snapshot:
            continue
        if not _is_vlm_available():
            continue
        entry: dict = {
            "label": "periodic",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "vlm_answers": [],
            "periodic": True,
        }
        answers = []
        for q in questions_snapshot:
            try:
                answer = _call_vlm(frame_bytes, q)
                answers.append({"question": q, "answer": answer})
                logger.info(f"VLM PERIODIC Q: {q} | A: {answer}")
            except _VlmUnavailable:
                break
            except Exception as exc:
                logger.error(f"VLM periodic error: {exc}")
        if answers:
            entry["vlm_answers"] = answers
            with detection_lock:
                detection_log.append(entry)
            with vlm_answers_lock:
                vlm_answers_log.append({
                    "label": entry.get("label", "periodic"),
                    "confidence": 0,
                    "timestamp": entry.get("timestamp", ""),
                    "vlm_answers": list(answers),
                    "periodic": True,
                })


# ── Detection log ─────────────────────────────────────────────────────────────

def _update_detection_log(results, frame=None) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    class_updates: list[str] = []
    with detection_lock:
        for result in results:
            for box in result.boxes:
                cls_id = int(box.cls[0])
                names = model.names
                cls_name = names.get(cls_id, str(cls_id)) if isinstance(names, dict) else names[cls_id]
                conf = float(box.conf[0])
                if conf >= CONF:
                    class_updates.append(cls_name)
                    bbox_coords = [round(v, 1) for v in box.xyxy[0].tolist()]
                    entry: dict = {
                        "label": cls_name,
                        "confidence": round(conf * 100, 1),
                        "timestamp": timestamp,
                        "bbox": bbox_coords,
                        "vlm_answers": [],
                    }
                    detection_log.append(entry)
                    # Log high-confidence detections only (reduces log volume from ~100/s to ~5/s)
                    if conf >= 0.8:
                        logger.info(f"DETECTION {cls_name} {round(conf * 100, 1)}%")
                    # Store high-confidence crops only if someone is reading them
                    # (conditional encoding — skip if no client read crops in last 60s)
                    _crops_active = (time.monotonic() - _last_crop_read) < 60.0
                    with crop_conf_lock:
                        _crop_threshold = crop_conf_threshold
                    if _crops_active and frame is not None and conf >= _crop_threshold:
                        try:
                            x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
                            h, w = frame.shape[:2]
                            crop = frame[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
                            if crop.size > 0:
                                crop_id = str(uuid.uuid4())[:12]
                                _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
                                crop_log.append({
                                    "id": crop_id,
                                    "label": cls_name,
                                    "confidence": round(conf * 100, 1),
                                    "timestamp": timestamp,
                                    "bbox": [x1, y1, x2, y2],
                                    "image": base64.b64encode(buf.tobytes()).decode("ascii"),
                                })
                                # Store full frame for on-demand retrieval
                                _, frame_buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                                crop_frames[crop_id] = frame_buf.tobytes()
                                # Evict oldest frames if over limit
                                while len(crop_frames) > CROP_FRAMES_MAX:
                                    oldest = next(iter(crop_frames))
                                    del crop_frames[oldest]
                        except Exception:
                            pass
                    with vlm_conf_lock:
                        _threshold = vlm_conf_threshold
                    with vlm_classes_lock:
                        _vlm_cls = vlm_classes
                    if VLM_ENABLED and frame is not None and conf >= _threshold and (not _vlm_cls or cls_name in _vlm_cls):
                        # Rate limit: don't queue VLM calls faster than every N seconds
                        global _vlm_last_detection_call
                        if time.monotonic() - _vlm_last_detection_call < _VLM_MIN_DETECTION_INTERVAL:
                            continue
                        # Spatial cache: skip if same class at same grid position was described recently
                        bbox_ints = [int(v) for v in box.xyxy[0].tolist()]
                        if _vlm_check_spatial_cache(cls_name, bbox_ints):
                            continue
                        with vlm_questions_lock:
                            questions_snapshot = [q["question"] for q in vlm_questions]
                        if questions_snapshot:
                            try:
                                x1, y1, x2, y2 = bbox_ints
                                h, w = frame.shape[:2]
                                crop = frame[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
                                if crop.size > 0:
                                    _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
                                    try:
                                        _vlm_queue.put_nowait((entry, buf.tobytes(), questions_snapshot))
                                        _vlm_last_detection_call = time.monotonic()
                                        _vlm_mark_spatial_cache(cls_name, bbox_ints)
                                    except _queue.Full:
                                        pass
                            except Exception:
                                pass
    if class_updates:
        with class_stats_lock:
            for cls_name in class_updates:
                if cls_name in class_stats:
                    class_stats[cls_name]["count"] += 1
                    class_stats[cls_name]["last_seen"] = timestamp


# ── Inference loop ────────────────────────────────────────────────────────────

def _warmup_model() -> None:
    if WARMUP_ITERS <= 0:
        return
    dummy = np.zeros((IMG_SIZE, IMG_SIZE, 3), dtype=np.uint8)
    with torch.inference_mode():
        for _ in range(WARMUP_ITERS):
            model.predict(source=dummy, device=DEVICE, imgsz=IMG_SIZE, half=HALF, conf=CONF, verbose=False)


def _inference_loop() -> None:
    global video_fps, inference_fps
    retry_delay = 0.5

    _warmup_model()

    while not stop_event.is_set():
        cap, source = _open_capture()
        if cap is None or not cap.isOpened():
            status_frame = _make_status_frame("Webcam not available", f"Retrying in {retry_delay:.1f}s")
            _set_latest_frame(_encode_frame(status_frame))
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 5.0)
            continue

        logger.info(f"Camera opened: {source}")
        with _active_cap_lock:
            global _active_cap
            _active_cap = cap
        retry_delay = 0.5
        frame_times = []
        infer_times = []

        try:
            frame_count = 0
            while not stop_event.is_set():
                start = time.monotonic()

                read_start = time.monotonic()
                ret, frame = cap.read()
                read_elapsed = time.monotonic() - read_start
                if read_elapsed > 2.0:
                    logger.warning(f"cap.read() took {read_elapsed:.1f}s — possible camera stall")

                if not ret:
                    logger.warning("Camera disconnected, reconnecting...")
                    break

                frame = _apply_software_zoom(frame)

                frame_count += 1
                if frame_count % 300 == 0:
                    logger.info(f"Inference loop alive — frame {frame_count}, threads={threading.active_count()}")

                frame_times.append(time.monotonic())
                if len(frame_times) > 30:
                    frame_times.pop(0)
                if len(frame_times) >= 2:
                    with fps_lock:
                        video_fps = round(len(frame_times) / (frame_times[-1] - frame_times[0]), 1)

                if pause_event.is_set():
                    _set_latest_frame(_encode_frame(frame))
                    time.sleep(0.1)
                    continue

                infer_start = time.monotonic()
                with torch.inference_mode():
                    results = model.predict(
                        source=frame, device=DEVICE, imgsz=IMG_SIZE, half=HALF, conf=CONF, verbose=False
                    )
                infer_elapsed = time.monotonic() - infer_start
                if infer_elapsed > 5.0:
                    logger.warning(f"model.predict() took {infer_elapsed:.1f}s — GPU may be contended")

                infer_times.append(time.monotonic())
                if len(infer_times) > 30:
                    infer_times.pop(0)
                if len(infer_times) >= 2:
                    with fps_lock:
                        inference_fps = round(len(infer_times) / (infer_times[-1] - infer_times[0]), 1)

                _update_detection_log(results, frame)
                _update_class_visibility(results)

                try:
                    output_frame = results[0].plot() if DRAW_OVERLAY else frame
                except KeyError:
                    # Custom models may have unmapped class IDs — fall back to raw frame
                    output_frame = frame
                _set_latest_frame(_encode_frame(output_frame))

                if TARGET_FPS > 0:
                    elapsed = time.monotonic() - start
                    delay = max(0.0, (1.0 / TARGET_FPS) - elapsed)
                    if delay > 0:
                        time.sleep(delay)
        finally:
            logger.warning("Inference loop exited — releasing camera")
            with _active_cap_lock:
                _active_cap = None
            cap.release()


def _hw_monitor_loop():
    """Log hardware metrics every 30s for post-crash analysis."""
    while not stop_event.is_set():
        try:
            mem = psutil.virtual_memory()
            swap = psutil.swap_memory()
            cpu = psutil.cpu_percent(interval=0.5)
            # NOTE: GPU sysfs reads removed — reading /sys/devices/.../gpu/load
            # while CUDA inference is active causes SIGTRAP on Jetson Orin.
            temps = {}
            for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
                try:
                    name = open(f"{zone}/type").read().strip()
                    temp = round(int(open(f"{zone}/temp").read().strip()) / 1000, 1)
                    if temp > 0 and name in ("cpu-thermal", "gpu-thermal", "tj-thermal"):
                        temps[name] = temp
                except Exception:
                    pass
            logger.info(
                f"HW: RAM={mem.used // 1024 // 1024}MB/{mem.total // 1024 // 1024}MB ({mem.percent}%) "
                f"SWAP={swap.used // 1024 // 1024}MB "
                f"CPU={cpu}% "
                f"temps={temps} "
                f"threads={threading.active_count()}"
            )
        except Exception as exc:
            logger.error(f"HW monitor error: {exc}")
        stop_event.wait(30)


def _safe_inference_loop():
    """Wrapper that catches and logs any exception from the inference loop."""
    try:
        _inference_loop()
    except Exception as exc:
        logger.critical(f"Inference loop crashed: {exc}", exc_info=True)
    logger.critical("Inference thread exiting — this should not happen")


def _start_inference_thread() -> None:
    global inference_thread
    if inference_thread and inference_thread.is_alive():
        return
    inference_thread = threading.Thread(target=_safe_inference_loop, daemon=True)
    inference_thread.start()


def _watchdog_loop():
    """Monitor inference thread and restart it if it dies."""
    while not stop_event.is_set():
        stop_event.wait(10)  # check every 10 seconds
        if stop_event.is_set():
            break
        if inference_thread and not inference_thread.is_alive():
            logger.warning("WATCHDOG: Inference thread died — restarting")
            _start_inference_thread()


# ── Hardware stats ────────────────────────────────────────────────────────────

_SKIP_IFACE_PREFIXES = ("lo", "docker", "veth", "br-", "virbr", "bond", "dummy", "sit", "tunl", "ip6tnl",
                        "utun", "ipsec", "anpi", "bridge", "ap", "awdl", "llw", "gif", "stf", "XHC")


def _network_info() -> list[dict]:
    wifi_signal: dict[str, dict] = {}
    try:
        with open("/proc/net/wireless") as f:
            for line in f.readlines()[2:]:
                parts = line.split()
                if not parts:
                    continue
                iface = parts[0].rstrip(":")
                try:
                    signal_dbm = int(parts[3].rstrip("."))
                    quality_pct = max(0, min(100, (signal_dbm + 100) * 2))
                    wifi_signal[iface] = {"signal_dbm": signal_dbm, "quality_pct": quality_pct}
                except (ValueError, IndexError):
                    wifi_signal[iface] = {}
    except OSError:
        pass

    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    result = []
    for iface, stat in stats.items():
        if any(iface.startswith(p) for p in _SKIP_IFACE_PREFIXES) or not stat.isup:
            continue
        ip = None
        for addr in addrs.get(iface, []):
            if addr.family == 2 and not addr.address.startswith("169.254"):
                ip = addr.address
                break
        if ip is None:
            for addr in addrs.get(iface, []):
                if addr.family == 2:
                    ip = addr.address
                    break
        if ip is None:
            continue  # skip interfaces with no IP
        is_wifi = iface in wifi_signal or Path(f"/sys/class/net/{iface}/wireless").exists()
        entry: dict = {"iface": iface, "type": "wifi" if is_wifi else "ethernet", "ip": ip}
        if is_wifi and iface in wifi_signal:
            entry.update(wifi_signal[iface])
        elif not is_wifi and stat.speed > 0:
            entry["speed_mbps"] = stat.speed
        result.append(entry)
    return result


def hardware_stats() -> dict:
    cpu_pct = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    # NOTE: GPU sysfs reads removed — causes SIGTRAP on Jetson Orin during CUDA inference.
    temps = {}
    for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
        try:
            name = open(f"{zone}/type").read().strip()
            temp = round(int(open(f"{zone}/temp").read().strip()) / 1000, 1)
            if temp > 0:
                temps[name] = temp
        except Exception:
            pass
    with fps_lock:
        v_fps = video_fps
        i_fps = inference_fps
    return {
        "cpu_pct": cpu_pct,
        "ram_used_gb": round(mem.used / 1024**3, 1),
        "ram_total_gb": round(mem.total / 1024**3, 1),
        "ram_pct": mem.percent,
        "gpu_pct": None,
        "temps": temps,
        "video_fps": v_fps,
        "inference_fps": i_fps,
        "network": _network_info(),
    }


# ── Video feed generator ──────────────────────────────────────────────────────

async def video_feed_generator():
    """Async MJPEG generator — runs on the event loop, never blocks a thread pool worker."""
    last_id = 0
    stale_seconds = 0.0
    poll_interval = 0.05  # 50ms → 20 Hz check rate, enough for 15 FPS delivery
    while True:
        current_id = latest_frame_id
        if current_id == last_id:
            stale_seconds += poll_interval
            if stale_seconds > 5.0:
                logger.warning("Video feed generator: no new frames for 5s, closing")
                return
            await asyncio.sleep(poll_interval)
            continue
        frame_bytes = latest_frame_bytes
        last_id = current_id
        stale_seconds = 0.0
        if not frame_bytes:
            await asyncio.sleep(poll_interval)
            continue
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"


# ── Public getters ────────────────────────────────────────────────────────────

def get_latest_frame_bytes() -> bytes | None:
    return latest_frame_bytes


def get_vlm_answers(limit: int = 50) -> list:
    """Return VLM answers from the persistent log."""
    with vlm_answers_lock:
        return list(vlm_answers_log)[-limit:]


def get_detections() -> list:
    with detection_lock:
        all_entries = list(detection_log)
    # Always include entries with VLM answers so they don't get lost
    with_vlm = [e for e in all_entries if e.get("vlm_answers")]
    recent = all_entries[-20:]
    # Merge: recent detections + any older ones with VLM answers not already in recent
    recent_set = set(id(e) for e in recent)
    extra_vlm = [e for e in with_vlm if id(e) not in recent_set]
    return extra_vlm + recent


def get_crops(limit: int = 20) -> list:
    global _last_crop_read
    _last_crop_read = time.monotonic()
    with detection_lock:
        return list(crop_log)[-limit:]


def get_crop_frame(crop_id: str) -> bytes | None:
    global _last_crop_read
    _last_crop_read = time.monotonic()
    with detection_lock:
        return crop_frames.get(crop_id)


def set_conf(value: float) -> None:
    global CONF
    CONF = max(0.0, min(1.0, float(value)))


def get_crop_config() -> dict:
    with crop_conf_lock:
        return {"conf_threshold": crop_conf_threshold}


def set_crop_config(conf_threshold: float) -> dict:
    global crop_conf_threshold
    val = max(0.0, min(1.0, float(conf_threshold)))
    with crop_conf_lock:
        crop_conf_threshold = val
    return {"conf_threshold": val}


def get_classes() -> dict:
    now = datetime.now(timezone.utc)
    with class_stats_lock:
        return {
            cls_name: {
                "count": stats["count"],
                "last_seen": stats["last_seen"],
                "seconds_visible": round((now - stats["visible_since"]).total_seconds(), 1)
                if stats["visible_since"] is not None else None,
            }
            for cls_name, stats in class_stats.items()
        }


def get_pause_state() -> bool:
    return pause_event.is_set()


def toggle_pause() -> bool:
    if pause_event.is_set():
        pause_event.clear()
        logger.info("Inference resumed")
        return False
    else:
        pause_event.set()
        logger.info("Inference paused")
        return True


def get_vlm_questions() -> list:
    with vlm_questions_lock:
        return list(vlm_questions)


def add_vlm_question(question: str) -> dict:
    entry = {"id": str(uuid.uuid4())[:8], "question": question}
    with vlm_questions_lock:
        vlm_questions.append(entry)
    return entry


def remove_vlm_question(qid: str) -> bool:
    with vlm_questions_lock:
        for i, q in enumerate(vlm_questions):
            if q["id"] == qid:
                vlm_questions.pop(i)
                return True
    return False


def get_vlm_config() -> dict:
    with vlm_interval_lock:
        interval = vlm_interval
    with vlm_conf_lock:
        conf = vlm_conf_threshold
    with vlm_classes_lock:
        classes = sorted(vlm_classes)
    with vlm_status_lock:
        connected = vlm_connected
        last_error = vlm_last_error
    return {
        "interval": interval,
        "conf_threshold": conf,
        "classes": classes,
        "connected": connected,
        "last_error": last_error,
        "url": VLM_URL,
        "consecutive_failures": _vlm_consecutive_failures,
        "auto_disabled": _vlm_consecutive_failures >= _VLM_MAX_CONSECUTIVE_FAILURES,
    }


def set_vlm_config(interval: float | None = None, conf_threshold: float | None = None, classes: list[str] | None = None) -> dict:
    global vlm_interval, vlm_conf_threshold
    result = {}
    if interval is not None:
        val = max(0.0, float(interval))
        with vlm_interval_lock:
            vlm_interval = val
        vlm_interval_changed.set()
        result["interval"] = val
    if conf_threshold is not None:
        val = max(0.0, min(1.0, float(conf_threshold)))
        with vlm_conf_lock:
            vlm_conf_threshold = val
        result["conf_threshold"] = val
    if classes is not None:
        with vlm_classes_lock:
            vlm_classes = set(c.strip() for c in classes if c.strip())
        result["classes"] = sorted(vlm_classes)
    return result


async def vlm_ask_once(question: str, include_image: bool = False) -> dict:
    frame_bytes = latest_frame_bytes
    if frame_bytes is None:
        raise RuntimeError("no frame available yet")
    loop = asyncio.get_event_loop()
    answer = await loop.run_in_executor(None, _call_vlm, frame_bytes, question)
    result: dict = {"question": question, "answer": answer}
    if include_image and frame_bytes:
        result["image"] = base64.b64encode(frame_bytes).decode("ascii")
    return result


def _get_camera_names() -> dict[int, str]:
    """Get camera names via platform-specific methods."""
    names: dict[int, str] = {}
    if platform.system() == "Darwin":
        try:
            import subprocess
            result = subprocess.run(
                ["system_profiler", "SPCameraDataType"],
                capture_output=True, text=True, timeout=5,
            )
            # Parse camera names in order — maps to OpenCV indices
            idx = 0
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.endswith(":") and not line.startswith("Camera:") and "Model ID" not in line and "Unique ID" not in line:
                    names[idx] = line.rstrip(":")
                    idx += 1
        except Exception:
            pass
    elif platform.system() == "Linux":
        try:
            import subprocess, glob as _glob
            for path in sorted(_glob.glob("/dev/video*")):
                idx = int(path.replace("/dev/video", ""))
                result = subprocess.run(
                    ["v4l2-ctl", "-d", path, "--info"],
                    capture_output=True, text=True, timeout=2,
                )
                for l in result.stdout.splitlines():
                    if "Card type" in l:
                        names[idx] = l.split(":", 1)[1].strip()
                        break
        except Exception:
            pass
    return names


def list_camera_indices(max_check: int = 5) -> list[dict]:
    """Probe camera indices and return which ones are available."""
    cam_names = _get_camera_names()
    results = []
    for i in range(max_check):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            name = cam_names.get(i, f"Camera {i}")
            results.append({"index": i, "name": name, "resolution": f"{w}x{h}"})
            cap.release()
        else:
            cap.release()
    return results


def get_camera_index() -> int:
    return CAMERA_INDEX


def set_camera_index(index: int) -> int:
    global CAMERA_INDEX
    CAMERA_INDEX = int(index)
    # Restart the inference loop to pick up the new camera
    stop_event.set()
    if inference_thread and inference_thread.is_alive():
        inference_thread.join(timeout=10)
    stop_event.clear()
    _start_inference_thread()
    logger.info(f"Switched to camera index {CAMERA_INDEX}")
    return CAMERA_INDEX


_CAM_PROPS = {
    "zoom": cv2.CAP_PROP_ZOOM,
    "pan": cv2.CAP_PROP_PAN,
    "tilt": cv2.CAP_PROP_TILT,
    "focus": cv2.CAP_PROP_FOCUS,
    "autofocus": cv2.CAP_PROP_AUTOFOCUS,
    "exposure": cv2.CAP_PROP_EXPOSURE,
    "auto_exposure": cv2.CAP_PROP_AUTO_EXPOSURE,
    "brightness": cv2.CAP_PROP_BRIGHTNESS,
    "contrast": cv2.CAP_PROP_CONTRAST,
    "saturation": cv2.CAP_PROP_SATURATION,
}


def get_camera_controls() -> dict:
    """Read current camera control values from the active capture."""
    with _active_cap_lock:
        cap = _active_cap
    if cap is None:
        return {"error": "no active camera"}
    result = {}
    for name, prop_id in _CAM_PROPS.items():
        result[name] = cap.get(prop_id)
    return result


def set_camera_control(name: str, value: float) -> dict:
    """Set a camera control value on the active capture."""
    with _active_cap_lock:
        cap = _active_cap
    if cap is None:
        return {"error": "no active camera"}
    prop_id = _CAM_PROPS.get(name)
    if prop_id is None:
        return {"error": f"unknown control: {name}", "available": list(_CAM_PROPS.keys())}
    success = cap.set(prop_id, value)
    actual = cap.get(prop_id)
    logger.info(f"Camera control {name}={value} -> success={success}, actual={actual}")
    return {"name": name, "requested": value, "actual": actual, "success": success}


def get_software_zoom() -> dict:
    with software_zoom_lock:
        return {"zoom": software_zoom, "pan": software_pan, "tilt": software_tilt}


def set_software_zoom(zoom: float | None = None, pan: float | None = None, tilt: float | None = None) -> dict:
    global software_zoom, software_pan, software_tilt
    with software_zoom_lock:
        if zoom is not None:
            software_zoom = max(1.0, min(10.0, float(zoom)))
        if pan is not None:
            software_pan = max(-1.0, min(1.0, float(pan)))
        if tilt is not None:
            software_tilt = max(-1.0, min(1.0, float(tilt)))
        result = {"zoom": software_zoom, "pan": software_pan, "tilt": software_tilt}
    logger.info(f"Software zoom: {result}")
    return result


def get_target_fps() -> float:
    return TARGET_FPS


def set_target_fps(fps: float) -> float:
    global TARGET_FPS
    TARGET_FPS = max(0.0, float(fps))
    return TARGET_FPS


def get_models() -> dict:
    return {
        "models": _list_available_models(),
        "active": active_model_name,
        "trt_status": trt_export_status,
    }


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def start() -> None:
    if not DETECTION_AVAILABLE:
        logger.warning("Detection deps not available, skipping detector startup.")
        return
    global model
    if model is None:
        model = _load_model()
        try:
            model.fuse()
        except Exception:
            pass
        if hasattr(torch, "set_float32_matmul_precision"):
            torch.set_float32_matmul_precision("high")
        torch.backends.cudnn.benchmark = True
    _init_class_stats()
    _start_inference_thread()
    # Pre-export engines for every other .pt now, in the background, so the
    # first profile switch hits a cached engine instead of a 30-60s slow path.
    threading.Thread(target=_prewarm_engines, daemon=True).start()
    if VLM_ENABLED:
        if VLM_QUESTION:
            with vlm_questions_lock:
                if not any(q["question"] == VLM_QUESTION for q in vlm_questions):
                    vlm_questions.append({"id": "default", "question": VLM_QUESTION})
        threading.Thread(target=_vlm_worker, daemon=True).start()
        threading.Thread(target=_vlm_periodic_thread, daemon=True).start()
        logger.info(f"VLM worker started (url={VLM_URL}, threshold={VLM_CONF_THRESHOLD})")
    threading.Thread(target=_hw_monitor_loop, daemon=True).start()
    threading.Thread(target=_watchdog_loop, daemon=True).start()
    logger.info("Detector started.")


def stop() -> None:
    stop_event.set()
    logger.info("Detector stopped.")
