#!/usr/bin/env python3
"""detector -- open-vocabulary object detection (YOLOE) for the Go2 inspection scan.

Two properties matter for this template:
  1. The prompt list is RUNTIME-configurable. `Detector.set_prompts(...)` re-encodes the
     text embeddings and swaps the open-vocab classes live, so the operator can retarget
     detection from the web UI without a redeploy.
  2. `imgsz` defaults to 640 (env YOLO_IMGSZ) — the dog's WebRTC camera is already ~720p,
     so upsampling buys little and costs a lot on CPU.

YOLOE text-prompt mode (ultralytics 8.x API):
    from ultralytics import YOLOE
    model = YOLOE("yoloe-11s-seg.pt")
    model.set_classes(names, model.get_text_pe(names))   # encode the open-vocab prompts
predict() then emits class ids that index into `names` (the current prompt order).

`get_text_pe` needs a CLIP/MobileCLIP text backend; the Dockerfile bakes both it and the
weights at build time so no download happens at runtime. Embeddings are cached on disk
(keyed by the prompt strings) next to the weights so repeated prompt sets are instant.
"""

import hashlib
import os
import threading

import cv2
import numpy as np

# Generic starter prompts. These are just defaults — the operator overrides them at runtime
# via POST /api/prompts. Order defines the class ids; the exact wording is what gets encoded,
# so retargeting means replacing the whole list, not editing spelling in place.
DEFAULT_PROMPTS = [
    "person",
    "fire extinguisher",
    "exit sign",
    "box",
    "chair",
    "laptop",
    "backpack",
    "spill on the floor",
]

# predict() params. `conf` is passed per-call. agnostic_nms MUST be forwarded False explicitly
# because YOLOE forces it True internally otherwise.
TUNED = {
    "iou": 0.59,
    "max_det": 21,
    "agnostic_nms": False,
    "retina_masks": False,
    "augment": False,
    "single_cls": False,
}

DEFAULT_WEIGHTS = os.environ.get(
    "YOLOE_WEIGHTS", os.path.expanduser("~/weights/yoloe-11s-seg.pt")
)
DEFAULT_IMGSZ = int(os.environ.get("YOLO_IMGSZ", "640"))

# Stable BGR colour per semantic group (hazards red, people orange, safety yellow, else green).
_RED = (40, 40, 220)
_ORANGE = (0, 140, 255)
_YELLOW = (0, 220, 220)
_GREEN = (60, 200, 60)


def color_for(name):
    n = name.lower()
    # Safety equipment / markers first (so "red fire extinguisher" reads as yellow, not red).
    if any(k in n for k in ("extinguisher", "hydrant", "exit", "cone", "barrier", "barier")):
        return _YELLOW
    if any(k in n for k in ("fire", "flame", "fumes", "smoke", "ash", "barrel", "electrical")):
        return _RED
    if "person" in n or "human" in n:
        return _ORANGE
    return _GREEN


def _pe_cache_path(weights, prompts):
    h = hashlib.md5("|".join(prompts).encode()).hexdigest()[:10]
    return os.path.join(os.path.dirname(weights) or ".", f"yoloe_pe_{h}.pt")


def _text_pe(model, weights, prompts):
    """Compute (or load cached) text prompt embeddings for `prompts`."""
    import torch

    cache = _pe_cache_path(weights, prompts)
    pe = None
    if os.path.exists(cache):
        try:
            pe = torch.load(cache, map_location="cpu")
        except Exception:
            pe = None
    if pe is None:
        pe = model.get_text_pe(prompts)  # needs the CLIP/MobileCLIP text backend
        try:
            torch.save(pe.cpu(), cache)
        except Exception:
            pass
    return pe


def load_model(weights=None, device="", prompts=DEFAULT_PROMPTS):
    """Load YOLOE and set the open-vocab text prompts.

    Raises (so the caller can degrade gracefully) if ultralytics or the weights file are
    unavailable and no download is permitted."""
    weights = os.path.expanduser(weights or DEFAULT_WEIGHTS)
    if not os.path.exists(weights) and not os.environ.get("YOLOE_ALLOW_DOWNLOAD"):
        raise FileNotFoundError(
            f"YOLOE weights not found: {weights} "
            f"(set YOLOE_WEIGHTS=/abs/path or YOLOE_ALLOW_DOWNLOAD=1)"
        )
    from ultralytics import YOLOE  # ImportError if ultralytics missing

    names = list(prompts)
    model = YOLOE(weights)
    model.set_classes(names, _text_pe(model, weights, names))
    if device:
        try:
            model.to(device)
        except Exception:
            pass
    return model


def infer(model, img_bgr, prompts, conf=0.26, imgsz=None, device=""):
    """Run YOLOE on one BGR frame -> [(class_name, conf, [x0,y0,x1,y1]), ...] in pixel coords.

    imgsz is rounded to a multiple of 32 (a YOLO requirement)."""
    H, W = img_bgr.shape[:2]
    imgsz = DEFAULT_IMGSZ if imgsz is None else imgsz
    kw = {
        "conf": float(conf),
        "iou": float(TUNED["iou"]),
        "imgsz": int(((int(imgsz) + 31) // 32) * 32),
        "max_det": int(TUNED["max_det"]),
        "agnostic_nms": bool(TUNED["agnostic_nms"]),
        "retina_masks": bool(TUNED["retina_masks"]),
        "augment": bool(TUNED["augment"]),
        "single_cls": bool(TUNED["single_cls"]),
        "verbose": False,
    }
    if device:
        kw["device"] = device
    res = model.predict(img_bgr, **kw)
    out = []
    if not res or res[0].boxes is None or len(res[0].boxes) == 0:
        return out
    r = res[0]
    boxes = r.boxes.xyxy.cpu().numpy()
    cls_ids = r.boxes.cls.cpu().numpy().astype(int)
    confs = r.boxes.conf.cpu().numpy()
    for i, b in enumerate(boxes):
        x0, y0, x1, y1 = [int(v) for v in b]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(W, x1), min(H, y1)
        if x1 <= x0 or y1 <= y0:
            continue
        name = prompts[cls_ids[i]] if 0 <= cls_ids[i] < len(prompts) else str(cls_ids[i])
        out.append((name, float(confs[i]), [x0, y0, x1, y1]))
    return out


def contact_sheet(zone_dir, objects, ch=160):
    """Montage of one crop per object (objects: dicts with 'class' + 'crop' relative path)."""
    rows = [o for o in objects if o.get("crop")]
    if not rows:
        return None
    sheet = np.full((ch + 22, ch * len(rows), 3), 40, np.uint8)
    for k, o in enumerate(rows):
        c = cv2.imread(os.path.join(zone_dir, o["crop"]))
        if c is None:
            continue
        s = ch / max(c.shape[:2])
        c = cv2.resize(c, (max(1, int(c.shape[1] * s)), max(1, int(c.shape[0] * s))))
        sheet[: c.shape[0], k * ch : k * ch + c.shape[1]] = c
        cv2.putText(
            sheet,
            o["class"].split()[0][:9],
            (k * ch + 4, ch + 16),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            color_for(o["class"]),
            1,
        )
    out = os.path.join(zone_dir, "objects_contact_sheet.png")
    cv2.imwrite(out, sheet)
    return out


class Detector:
    """Threadsafe wrapper around a loaded YOLOE model + the current prompt set.

    The model is expensive to load and not safe to call concurrently from multiple threads
    (the annotated-stream loop and an /api/capture request can race), so every inference and
    every prompt swap is serialised behind one lock. Prompt swaps re-encode the text
    embeddings and call set_classes on the live model — no reload of the weights.
    """

    def __init__(self, weights=None, device="", prompts=None):
        self._weights = os.path.expanduser(weights or DEFAULT_WEIGHTS)
        self._device = device
        self._prompts = list(prompts or DEFAULT_PROMPTS)
        self._lock = threading.Lock()
        self._model = load_model(self._weights, device=device, prompts=self._prompts)

    @property
    def prompts(self):
        with self._lock:
            return list(self._prompts)

    def set_prompts(self, prompts):
        """Retarget detection to a new open-vocab prompt list (re-encodes embeddings)."""
        names = [p.strip() for p in prompts if p and p.strip()]
        if not names:
            raise ValueError("prompt list is empty")
        with self._lock:
            self._model.set_classes(names, _text_pe(self._model, self._weights, names))
            self._prompts = names
        return names

    def detect(self, img_bgr, conf=0.26, imgsz=None):
        """Run detection on one BGR frame under the current prompts."""
        with self._lock:
            return infer(
                self._model, img_bgr, self._prompts, conf=conf, imgsz=imgsz, device=self._device
            )
