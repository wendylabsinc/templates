#!/usr/bin/env python3
"""report -- draw detections on a frame and append captures to a running report.

The dog has no onboard SLAM map on this path, so there are no world coordinates: a report is
just annotated frames plus per-object crops. What this module does: draw boxes on a frame, and
append each capture to report.md + report.csv. Pure OpenCV — no ROS deps.

A "capture" is one on-demand snapshot: an annotated JPEG, a set of per-object crop JPEGs, and
a row per object in the report. Everything lands under CAPTURE_DIR.
"""

import csv
import os

import cv2

from detector import color_for

REPORT_MD = "report.md"
REPORT_CSV = "report.csv"


def draw_detections(img_bgr, detections):
    """Return a copy of `img_bgr` with each detection drawn as a class-coloured box + label.

    detections: [(class_name, conf, [x0, y0, x1, y1]), ...]."""
    out = img_bgr.copy()
    for name, conf, (x0, y0, x1, y1) in detections:
        col = color_for(name)
        cv2.rectangle(out, (x0, y0), (x1, y1), col, 2)
        label = f"{name} {conf:.2f}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        ty = max(0, y0 - th - 4)
        cv2.rectangle(out, (x0, ty), (x0 + tw + 6, ty + th + 6), col, -1)
        cv2.putText(
            out,
            label,
            (x0 + 3, ty + th + 1),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (20, 20, 20),
            1,
            cv2.LINE_AA,
        )
    return out


def _append_md(md_path, timestamp, objects, annotated_rel):
    """Append one capture section to report.md (creating the header on first write)."""
    new = not os.path.exists(md_path)
    with open(md_path, "a") as f:
        if new:
            f.write("# Go2 inspection report\n\n")
            f.write("On-demand captures from the dog's front camera, newest appended last.\n")
        f.write(f"\n## Capture {timestamp}\n\n")
        f.write(f"![capture]({annotated_rel})\n\n")
        f.write(f"**{len(objects)} object(s) detected.**\n\n")
        f.write("| # | class | confidence | crop |\n")
        f.write("|---|-------|-----------|------|\n")
        for i, o in enumerate(objects):
            f.write(
                f"| {i} | {o['class']} | {o['confidence']:.2f} | {o.get('crop', '')} |\n"
            )


def _append_csv(csv_path, timestamp, objects):
    """Append one row per detected object to report.csv (creating the header on first write)."""
    new = not os.path.exists(csv_path)
    with open(csv_path, "a", newline="") as f:
        wr = csv.writer(f)
        if new:
            wr.writerow(["timestamp", "class", "confidence", "x0", "y0", "x1", "y1", "crop"])
        for o in objects:
            b = o.get("box") or [None, None, None, None]
            wr.writerow(
                [timestamp, o["class"], round(o["confidence"], 3), b[0], b[1], b[2], b[3],
                 o.get("crop", "")]
            )


def save_capture(capture_dir, timestamp, frame_bgr, detections, jpeg_quality=80):
    """Persist one capture: annotated JPEG + per-object crops, append to report.md / report.csv.

    Returns a dict {timestamp, annotated, objects:[{class,confidence,box,crop}]} with paths
    relative to `capture_dir` (so they can be served under /captures/...)."""
    os.makedirs(capture_dir, exist_ok=True)
    enc = [cv2.IMWRITE_JPEG_QUALITY, int(jpeg_quality)]

    annotated_rel = f"{timestamp}_annotated.jpg"
    annotated = draw_detections(frame_bgr, detections)
    cv2.imwrite(os.path.join(capture_dir, annotated_rel), annotated, enc)

    objects = []
    for i, (name, conf, (x0, y0, x1, y1)) in enumerate(detections):
        crop_rel = f"{timestamp}_crop{i}.jpg"
        crop = frame_bgr[y0:y1, x0:x1]
        if crop.size > 0:
            cv2.imwrite(os.path.join(capture_dir, crop_rel), crop, enc)
        else:
            crop_rel = ""
        objects.append(
            {"class": name, "confidence": conf, "box": [x0, y0, x1, y1], "crop": crop_rel}
        )

    _append_md(os.path.join(capture_dir, REPORT_MD), timestamp, objects, annotated_rel)
    _append_csv(os.path.join(capture_dir, REPORT_CSV), timestamp, objects)

    return {"timestamp": timestamp, "annotated": annotated_rel, "objects": objects}


def load_report(capture_dir):
    """Read report.csv back into a list of capture dicts (newest first) for /api/report.

    Rows are grouped by timestamp; each capture keeps its annotated image + object list.
    Returns [] if nothing has been captured yet."""
    csv_path = os.path.join(capture_dir, REPORT_CSV)
    if not os.path.exists(csv_path):
        return []
    captures = {}
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            ts = row["timestamp"]
            cap = captures.setdefault(
                ts, {"timestamp": ts, "annotated": f"{ts}_annotated.jpg", "objects": []}
            )
            cap["objects"].append(
                {
                    "class": row["class"],
                    "confidence": float(row["confidence"]) if row["confidence"] else 0.0,
                    "crop": row.get("crop", ""),
                }
            )
    # Timestamps are zero-padded sortable strings; newest last in the file, so reverse.
    return sorted(captures.values(), key=lambda c: c["timestamp"], reverse=True)
