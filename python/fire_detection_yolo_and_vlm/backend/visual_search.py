"""Visual Search Agent — uses YOLO bboxes + VLM + software zoom/pan/tilt to find and track objects."""

import logging
import threading
import time

logger = logging.getLogger("visual_search")


class VisualSearchAgent:
    """Autonomously searches for objects using YOLO detections + VLM confirmation."""

    # Grid positions for VLM-only fallback scanning (pan, tilt)
    SCAN_GRID = [
        (0.0, 0.0),     # center
        (-0.7, 0.0),    # left
        (0.7, 0.0),     # right
        (0.0, -0.7),    # up
        (0.0, 0.7),     # down
        (-0.7, -0.7),   # top-left
        (0.7, -0.7),    # top-right
        (-0.7, 0.7),    # bottom-left
        (0.7, 0.7),     # bottom-right
    ]

    def __init__(self, detector_module):
        self._det = detector_module
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._status = "idle"
        self._question = ""
        self._log: list[dict] = []
        self._max_log = 50
        self._found_image: str | None = None  # base64 JPEG of the found object
        self._found_description: str | None = None

    def _add_log(self, step: str, detail: str, zoom: float = 0, pan: float = 0, tilt: float = 0):
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "step": step,
            "detail": detail,
            "zoom": round(zoom, 2),
            "pan": round(pan, 2),
            "tilt": round(tilt, 2),
        }
        self._log.append(entry)
        if len(self._log) > self._max_log:
            self._log.pop(0)
        logger.info(f"[VisualSearch] {step}: {detail} (z={zoom:.1f} p={pan:.1f} t={tilt:.1f})")

    def _mark_found(self, vlm_answer: str):
        """Set status to found, capture the current frame and VLM description."""
        import base64
        self._status = "found"
        frame_bytes = self._det.latest_frame_bytes
        if frame_bytes:
            self._found_image = base64.b64encode(frame_bytes).decode("ascii")
        # Extract description: everything after first line (YES/NO line)
        lines = vlm_answer.strip().split("\n")
        desc = "\n".join(lines[1:]).strip() if len(lines) > 1 else vlm_answer.strip()
        self._found_description = desc if desc else vlm_answer.strip()

    def _ask_vlm(self, question: str, image_bytes: bytes | None = None) -> str | None:
        """Send image to VLM with a question. Uses latest frame if no image provided."""
        if image_bytes is None:
            image_bytes = self._det.latest_frame_bytes
        if image_bytes is None:
            return None
        try:
            return self._det._call_vlm(image_bytes, question)
        except Exception as exc:
            logger.error(f"[VisualSearch] VLM error: {exc}")
            return None

    def _set_camera(self, zoom: float, pan: float, tilt: float):
        """Set software zoom/pan/tilt."""
        self._det.set_software_zoom(zoom=zoom, pan=pan, tilt=tilt)
        time.sleep(0.3)  # let a new frame come in with the new crop

    def _bbox_to_pan_tilt(self, x1: float, y1: float, x2: float, y2: float,
                          frame_w: float, frame_h: float) -> tuple[float, float]:
        """Convert a bounding box to pan/tilt values that center it.

        Returns (pan, tilt) in range [-1, 1] where 0,0 is center.
        """
        # Center of the bounding box as fraction of frame (0.0 to 1.0)
        cx = (x1 + x2) / 2 / frame_w
        cy = (y1 + y2) / 2 / frame_h
        # Convert to pan/tilt: 0.5 -> 0.0, 0.0 -> -1.0, 1.0 -> 1.0
        pan = (cx - 0.5) * 2.0
        tilt = (cy - 0.5) * 2.0
        return (max(-1.0, min(1.0, pan)), max(-1.0, min(1.0, tilt)))

    def _bbox_to_zoom(self, x1: float, y1: float, x2: float, y2: float,
                      frame_w: float, frame_h: float, fill_ratio: float = 0.6) -> float:
        """Calculate zoom level to make the bbox fill `fill_ratio` of the frame."""
        bbox_w = (x2 - x1) / frame_w
        bbox_h = (y2 - y1) / frame_h
        bbox_size = max(bbox_w, bbox_h)
        if bbox_size <= 0:
            return 1.0
        zoom = fill_ratio / bbox_size
        return max(1.0, min(5.0, zoom))

    def _get_yolo_detections(self) -> list[dict]:
        """Get current YOLO detections by reading from the detector's existing results.

        Uses the latest frame + detection log instead of running a separate
        model.predict() call, which would deadlock the GPU.
        """
        import cv2
        import numpy as np

        frame_bytes = self._det.latest_frame_bytes
        if frame_bytes is None:
            return []

        # Decode the latest JPEG frame to extract crops
        arr = np.frombuffer(frame_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return []

        h, w = frame.shape[:2]

        # Read recent detections from the log (already produced by inference loop)
        with self._det.detection_lock:
            recent = list(self._det.detection_log)

        # Get detections from the last 2 seconds
        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=2)
        detections = []
        seen_labels = set()

        for entry in reversed(recent):
            try:
                ts = datetime.fromisoformat(entry["timestamp"])
                if ts < cutoff:
                    break
            except (KeyError, ValueError):
                continue

            label = entry.get("label", "")
            conf = entry.get("confidence", 0)
            bbox = entry.get("bbox")

            # Skip duplicates (same class) and entries without bbox
            if not bbox or label in seen_labels:
                continue
            seen_labels.add(label)

            x1, y1, x2, y2 = [int(v) for v in bbox]
            crop = frame[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
            crop_bytes = None
            if crop.size > 0:
                _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
                crop_bytes = buf.tobytes()

            detections.append({
                "label": label,
                "confidence": conf,
                "bbox": [float(x1), float(y1), float(x2), float(y2)],
                "frame_size": [w, h],
                "crop_bytes": crop_bytes,
            })

        return detections

    def _is_yes(self, answer: str) -> bool:
        first_line = answer.strip().split("\n")[0].upper()
        return "YES" in first_line

    def _center_on_bbox(self, bbox: list[float], frame_size: list[float], target_zoom: float | None = None):
        """Zoom and center the camera on a bounding box."""
        x1, y1, x2, y2 = bbox
        fw, fh = frame_size
        pan, tilt = self._bbox_to_pan_tilt(x1, y1, x2, y2, fw, fh)
        if target_zoom is None:
            target_zoom = self._bbox_to_zoom(x1, y1, x2, y2, fw, fh)
        self._set_camera(target_zoom, pan, tilt)
        return target_zoom, pan, tilt

    def _search_loop(self, question: str):
        """Main search algorithm: YOLO first, VLM fallback."""
        try:
            self._run_search(question)
        finally:
            # Always reset zoom when search finishes
            self._set_camera(1.0, 0.0, 0.0)
            self._add_log("reset", "Camera reset to wide view", 1.0, 0.0, 0.0)

    def _run_search(self, question: str):
        self._status = "searching"
        self._add_log("start", f"Searching for: {question}")

        # Reset camera to wide view
        self._set_camera(1.0, 0.0, 0.0)
        time.sleep(0.5)

        # ── Phase 1: Check YOLO detections ──────────────────────────────────
        self._add_log("phase", "Phase 1: Checking YOLO detections")
        detections = self._get_yolo_detections()

        if detections:
            self._add_log("yolo", f"Found {len(detections)} YOLO detections, asking VLM to identify")

            confirm_prompt = (
                f'{question} '
                f'Answer with YES or NO on the first line. '
                f'If YES, describe what you see briefly.'
            )

            # Ask VLM about each detection crop
            for i, det in enumerate(detections):
                if self._stop.is_set():
                    self._status = "cancelled"
                    return

                if det["crop_bytes"] is None:
                    continue

                self._add_log("check", f"Checking {det['label']} ({det['confidence']}%)")
                answer = self._ask_vlm(confirm_prompt, image_bytes=det["crop_bytes"])
                if answer is None:
                    continue

                self._add_log("vlm", answer)

                if self._is_yes(answer):
                    # Found it — center on it
                    zoom, pan, tilt = self._center_on_bbox(det["bbox"], det["frame_size"])
                    self._add_log("found", f"Confirmed: {det['label']} ({det['confidence']}%)", zoom, pan, tilt)

                    # Re-confirm at zoomed view
                    time.sleep(0.5)
                    answer2 = self._ask_vlm(confirm_prompt)
                    if answer2 and self._is_yes(answer2):
                        self._add_log("result", f"Centered and confirmed", zoom, pan, tilt)
                        self._add_log("confirm", answer2, zoom, pan, tilt)
                        self._mark_found(answer2)
                        return
                    else:
                        # Try fine-tuning position
                        self._add_log("refine", "Zoomed view not confirmed, refining...")
                        # Get fresh YOLO detections at this zoom level
                        time.sleep(0.3)
                        zoomed_dets = self._get_yolo_detections()
                        for zd in zoomed_dets:
                            if zd["crop_bytes"] is None:
                                continue
                            ans = self._ask_vlm(confirm_prompt, image_bytes=zd["crop_bytes"])
                            if ans and self._is_yes(ans):
                                z2, p2, t2 = self._center_on_bbox(zd["bbox"], zd["frame_size"])
                                self._add_log("result", f"Refined and confirmed", z2, p2, t2)
                                self._add_log("confirm", ans, z2, p2, t2)
                                self._mark_found(ans)
                                return

            self._add_log("phase", "YOLO detections didn't match, trying VLM scan")

        # ── Phase 2: VLM-only wide scan ─────────────────────────────────────
        self._set_camera(1.0, 0.0, 0.0)
        time.sleep(0.3)

        locate_prompt = (
            f'Look at this image carefully. {question} '
            f'Answer with YES or NO on the first line. '
            f'If YES, estimate the position as X% from left and Y% from top '
            f'(e.g. "YES, approximately 30% from left, 60% from top").'
        )

        self._add_log("phase", "Phase 2: VLM wide scan")
        answer = self._ask_vlm(locate_prompt)
        if answer is None:
            self._add_log("error", "VLM unavailable")
            self._status = "error"
            return
        self._add_log("vlm", answer, 1.0, 0.0, 0.0)

        if self._is_yes(answer):
            pan, tilt = self._parse_percentage_position(answer)
            self._add_log("found", f"VLM detected at pan={pan:.2f}, tilt={tilt:.2f}")
            self._progressive_zoom(question, pan, tilt)
            return

        # ── Phase 3: Grid scan at 2x ────────────────────────────────────────
        self._add_log("phase", "Phase 3: Grid scan at 2x zoom")
        for i, (scan_pan, scan_tilt) in enumerate(self.SCAN_GRID):
            if self._stop.is_set():
                self._add_log("stop", "Search cancelled")
                self._status = "cancelled"
                return

            self._set_camera(2.0, scan_pan, scan_tilt)
            self._add_log("scan", f"Grid position {i+1}/{len(self.SCAN_GRID)}", 2.0, scan_pan, scan_tilt)

            # Try YOLO + VLM at this position
            dets = self._get_yolo_detections()
            confirm_prompt = (
                f'{question} '
                f'Answer with YES or NO on the first line.'
            )
            for det in dets:
                if det["crop_bytes"] is None:
                    continue
                ans = self._ask_vlm(confirm_prompt, image_bytes=det["crop_bytes"])
                if ans and self._is_yes(ans):
                    zoom, pan, tilt = self._center_on_bbox(det["bbox"], det["frame_size"])
                    self._add_log("result", f"Found via grid YOLO+VLM at grid pos {i+1}", zoom, pan, tilt)
                    self._add_log("confirm", ans, zoom, pan, tilt)
                    self._mark_found(ans)
                    return

            # No YOLO match — ask VLM about the full frame
            answer = self._ask_vlm(locate_prompt)
            if answer and self._is_yes(answer):
                pan, tilt = self._parse_percentage_position(answer)
                # Adjust relative to current scan position
                adj_pan = max(-1.0, min(1.0, scan_pan + pan * 0.5))
                adj_tilt = max(-1.0, min(1.0, scan_tilt + tilt * 0.5))
                self._add_log("found", f"VLM detected during grid scan")
                self._progressive_zoom(question, adj_pan, adj_tilt)
                return

        # ── Phase 4: Not found ──────────────────────────────────────────────
        self._add_log("result", "Object not found after full scan")
        self._set_camera(1.0, 0.0, 0.0)
        self._status = "not_found"

    def _parse_percentage_position(self, answer: str) -> tuple[float, float]:
        """Parse VLM percentage position like '30% from left, 60% from top' into pan/tilt."""
        import re
        # Look for patterns like "30% from left" and "60% from top"
        x_match = re.search(r'(\d+)%\s*(?:from\s+(?:the\s+)?left|horizontally|x)', answer.lower())
        y_match = re.search(r'(\d+)%\s*(?:from\s+(?:the\s+)?top|vertically|y)', answer.lower())

        if x_match and y_match:
            x_pct = int(x_match.group(1)) / 100.0
            y_pct = int(y_match.group(1)) / 100.0
            pan = (x_pct - 0.5) * 2.0
            tilt = (y_pct - 0.5) * 2.0
            return (max(-1.0, min(1.0, pan)), max(-1.0, min(1.0, tilt)))

        # Fallback: try to find any two percentages
        pcts = re.findall(r'(\d+)%', answer)
        if len(pcts) >= 2:
            x_pct = int(pcts[0]) / 100.0
            y_pct = int(pcts[1]) / 100.0
            pan = (x_pct - 0.5) * 2.0
            tilt = (y_pct - 0.5) * 2.0
            return (max(-1.0, min(1.0, pan)), max(-1.0, min(1.0, tilt)))

        # Last fallback: direction-based
        return self._parse_direction(answer)

    def _parse_direction(self, answer: str) -> tuple[float, float]:
        """Fallback: parse coarse direction words."""
        DIRECTION_MAP = {
            "top-left": (-0.4, -0.4), "top-right": (0.4, -0.4),
            "bottom-left": (-0.4, 0.4), "bottom-right": (0.4, 0.4),
            "left": (-0.4, 0.0), "right": (0.4, 0.0),
            "top": (0.0, -0.4), "bottom": (0.0, 0.4),
            "center": (0.0, 0.0),
        }
        answer_lower = answer.lower()
        for direction, (dp, dt) in sorted(DIRECTION_MAP.items(), key=lambda x: -len(x[0])):
            if direction in answer_lower:
                return (dp, dt)
        return (0.0, 0.0)

    def _progressive_zoom(self, question: str, pan: float, tilt: float):
        """Progressively zoom in on a position: 1.5x → 2x → 3x, refining at each step."""
        confirm_prompt = (
            f'{question} '
            f'Answer YES or NO. If YES, describe what you see briefly.'
        )

        for zoom_level in [1.5, 2.5, 3.5]:
            if self._stop.is_set():
                self._status = "cancelled"
                return

            self._set_camera(zoom_level, pan, tilt)
            self._add_log("zoom", f"Progressive zoom {zoom_level}x", zoom_level, pan, tilt)
            time.sleep(0.3)

            # Check YOLO at this zoom — might give us a precise bbox to center on
            dets = self._get_yolo_detections()
            for det in dets:
                if det["crop_bytes"] is None:
                    continue
                ans = self._ask_vlm(confirm_prompt, image_bytes=det["crop_bytes"])
                if ans and self._is_yes(ans):
                    # Re-center precisely on this detection
                    z, p, t = self._center_on_bbox(det["bbox"], det["frame_size"], target_zoom=zoom_level + 0.5)
                    pan, tilt = p, t  # update for next zoom level
                    self._add_log("center", f"Centered on {det['label']} ({det['confidence']}%)", z, p, t)
                    self._add_log("confirm", ans, z, p, t)
                    if zoom_level >= 3.0:
                        self._add_log("result", "Found and centered", z, p, t)
                        self._mark_found(ans)
                        return
                    break  # move to next zoom level
            else:
                # No YOLO match — ask VLM about full frame at this zoom
                answer = self._ask_vlm(confirm_prompt)
                if answer is None:
                    continue
                self._add_log("vlm", answer, zoom_level, pan, tilt)
                if not self._is_yes(answer):
                    self._add_log("result", f"Lost target at zoom {zoom_level}x")
                    self._set_camera(1.0, 0.0, 0.0)
                    self._status = "not_found"
                    return

        # Final confirmation — ask VLM for a description
        desc_answer = self._ask_vlm(f"Describe what you see in this image. {question}")
        self._add_log("result", "Found and centered", zoom_level, pan, tilt)
        self._mark_found(desc_answer or "Object found")

    # ── Public API ───────────────────────────────────────────────────────────

    def start_search(self, question: str) -> dict:
        """Start an autonomous visual search."""
        with self._lock:
            if self._status == "searching":
                return {"error": "search already in progress", "status": self._status}

            self._stop.clear()
            self._question = question
            self._log = []
            self._found_image = None
            self._found_description = None
            self._thread = threading.Thread(target=self._search_loop, args=(question,), daemon=True)
            self._thread.start()
            return {"status": "started", "question": question}

    def stop_search(self) -> dict:
        """Cancel the current search."""
        self._stop.set()
        return {"status": "stopping"}

    def get_status(self, include_image: bool = False) -> dict:
        """Get current search status and log."""
        result = {
            "status": self._status,
            "question": self._question,
            "log": list(self._log),
        }
        if self._status == "found":
            result["description"] = self._found_description
            if include_image and self._found_image:
                result["image"] = f"data:image/jpeg;base64,{self._found_image}"
        return result
