"""Use-case profiles — each bundles a YOLO model, VLM questions, and thresholds."""

import os
import threading
from pathlib import Path

PROFILES = [
    {
        "id": "coco",
        "name": "General Detection",
        "description": "Detects 80 common object categories (COCO dataset)",
        "model": "yolov8n.pt",
        "yolo_conf": 0.6,
        "vlm_questions": [],
        "vlm_conf_threshold": 0.7,
        "vlm_interval": 0.0,
        "crop_conf_threshold": 0.7,
    },
    {
        "id": "fire",
        "name": "Fire Detection",
        "description": "Detects fire and smoke",
        "model": "fire.pt",
        "yolo_conf": 0.6,
        "vlm_questions": ["Is there  fire visible? Answer yes or no!"],
        "vlm_conf_threshold": 0.8,
        "vlm_interval": 55.0,
        "crop_conf_threshold": 0.8,
    },
    {
        "id": "water",
        "name": "Water Leak Detection",
        "description": "Detects water and water leakage",
        "model": "water.pt",
        "yolo_conf": 0.4,
        "vlm_questions": ["Is there a water leak or flooding visible? Describe the source and extent."],
        "vlm_conf_threshold": 0.5,
        "vlm_interval": 5.0,
        "crop_conf_threshold": 0.5,
    },
    {
        "id": "gauge",
        "name": "Gauge Reading",
        "description": "Detects level gauges and reads values via VLM",
        "model": "gauges.pt",
        "yolo_conf": 0.5,
        "vlm_questions": ["Read the gauge or meter in this image. What is the level or value? Report the reading."],
        "vlm_conf_threshold": 0.6,
        "vlm_interval": 10.0,
        "crop_conf_threshold": 0.6,
    },
]

active_profile_id: str = "coco"
profile_lock = threading.Lock()


def _model_available(model_name: str) -> bool:
    """Check if the model file exists in any of the search directories."""
    models_dir = Path(os.environ.get("YOLO_MODELS_DIR", "/yolo-models"))
    for directory in [Path("/app"), models_dir, Path(".")]:
        if (directory / model_name).exists():
            return True
    return False


def list_profiles() -> list[dict]:
    with profile_lock:
        current = active_profile_id
    result = []
    for p in PROFILES:
        result.append({
            **p,
            "active": p["id"] == current,
            "available": _model_available(p["model"]),
        })
    return result


def get_active_profile() -> dict | None:
    with profile_lock:
        pid = active_profile_id
    for p in PROFILES:
        if p["id"] == pid:
            return {**p, "active": True}
    return None


def set_active_profile(profile_id: str) -> dict | None:
    global active_profile_id
    for p in PROFILES:
        if p["id"] == profile_id:
            with profile_lock:
                active_profile_id = profile_id
            return p
    return None
