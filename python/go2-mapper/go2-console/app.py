#!/usr/bin/env python3
"""
go2-console — the operator's single page for a mapping session.

Endpoints:
  GET  /                     control panel UI (static/index.html)
  POST /session/start        start bag recording (SLAM runs continuously)
  POST /session/stop         stop bag recording
  GET  /session/status       recorder status + latest SLAM pose + map size
  POST /waypoint             mark current SLAM pose as a named waypoint
  GET  /waypoints            list marked waypoints (raw map frame)
  POST /refine               offline loop-closure refine of the map (refine.py)
  POST /export               run export.py on the latest (refined if present) map
  GET  /exports              list finished exports
  GET  /download/{session}.zip           everything, zipped
  GET  /download/{session}/{filename}    individual artifact

Pose intake: reads /go2/slam/pose_json (std_msgs/String JSON published by
go2-slam's viz_bridge) via bare cyclonedds-python — the same pattern
go2-foxglove uses, so no rclpy/ROS install in this image.
"""

import io
import json
import os
import threading
import time
import zipfile
from dataclasses import dataclass

import httpx
from cyclonedds.domain import DomainParticipant
from cyclonedds.idl import IdlStruct
from cyclonedds.sub import DataReader, Subscriber
from cyclonedds.topic import Topic
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

import export as exporter
import refine as refiner

RECORDER_URL = os.environ.get("RECORDER_URL", "http://127.0.0.1:3610")
MAP_SAVE_DIR = os.environ.get("MAP_SAVE_DIR", "/data/maps")
EXPORT_DIR = os.environ.get("EXPORT_DIR", "/data/exports")
STATIC = os.path.join(os.path.dirname(__file__), "static")

CURRENT = os.path.join(MAP_SAVE_DIR, "current")
KEYFRAME_DIR = os.path.join(CURRENT, "keyframes")
RAW_PCD = os.path.join(CURRENT, "map.pcd")
REFINED_PCD = os.path.join(CURRENT, "map_refined.pcd")
BAG_DIR = os.environ.get("BAG_DIR", "/data/bags")  # shared with go2-recorder

app = FastAPI()


# --------------------------------------------------- DDS pose subscriber
# Matches ROS2's on-wire std_msgs/String: single `data` field, `rt/` prefix.
# @dataclass is REQUIRED — without it cyclonedds can't build the type
# (AttributeError: 'NoneType' has no attribute 'SupportsBasic').
@dataclass
class StdString(IdlStruct, typename="std_msgs::msg::dds_::String_"):
    data: str = ""


class PoseListener:
    def __init__(self):
        self.latest = None
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        try:
            dp = DomainParticipant()
            topic = Topic(dp, "rt/go2/slam/pose_json", StdString)
            reader = DataReader(Subscriber(dp), topic)
        except Exception as e:  # DDS init failure — degrade, don't crash the web app
            print(f"[pose-listener] disabled: {e}", flush=True)
            return
        while True:
            for sample in reader.take_iter(timeout=1_000_000_000):
                try:
                    self.latest = {**json.loads(sample.data),
                                   "received_ns": time.monotonic_ns()}
                except (json.JSONDecodeError, AttributeError):
                    pass


class HealthListener:
    """Reads go2-slam's /go2/slam/health_json so the console can surface a
    divergence banner instead of silently exporting a blown-up map. [#3]"""

    def __init__(self):
        self.latest = {"status": "unknown", "reason": None}
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        try:
            dp = DomainParticipant()
            topic = Topic(dp, "rt/go2/slam/health_json", StdString)
            reader = DataReader(Subscriber(dp), topic)
        except Exception as e:  # DDS init failure — degrade, don't crash the web app
            print(f"[health-listener] disabled: {e}", flush=True)
            return
        while True:
            for sample in reader.take_iter(timeout=1_000_000_000):
                try:
                    self.latest = json.loads(sample.data)
                except (json.JSONDecodeError, AttributeError):
                    pass


pose = PoseListener()
health = HealthListener()
waypoints: list[dict] = []
WAYPOINTS_FILE = os.path.join(MAP_SAVE_DIR, "current", "waypoints_raw.json")


def _persist_waypoints():
    os.makedirs(os.path.dirname(WAYPOINTS_FILE), exist_ok=True)
    with open(WAYPOINTS_FILE, "w") as f:
        json.dump(waypoints, f, indent=2)


if os.path.exists(WAYPOINTS_FILE):
    with open(WAYPOINTS_FILE) as f:
        waypoints = json.load(f)


# ------------------------------------------------------------- endpoints
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC, "index.html")) as f:
        return f.read()


@app.post("/session/start")
def session_start():
    r = httpx.post(f"{RECORDER_URL}/start", timeout=10)
    return r.json()


@app.post("/session/stop")
def session_stop():
    r = httpx.post(f"{RECORDER_URL}/stop", timeout=30)
    return r.json()


@app.get("/session/status")
def session_status():
    try:
        rec = httpx.get(f"{RECORDER_URL}/status", timeout=5).json()
    except httpx.HTTPError:
        rec = {"recording": False, "error": "recorder unreachable"}
    map_pcd = os.path.join(MAP_SAVE_DIR, "current", "map.pcd")
    stale_s = None
    if pose.latest:
        stale_s = (time.monotonic_ns() - pose.latest["received_ns"]) / 1e9
    return {
        "recorder": rec,
        "pose": pose.latest,
        "pose_stale_s": stale_s,
        "health": health.latest,
        "map_snapshot_bytes": os.path.getsize(map_pcd) if os.path.exists(map_pcd) else 0,
        "keyframes": len(_list_keyframes()),
        "refined": os.path.exists(REFINED_PCD),
        "waypoints": len(waypoints),
    }


@app.post("/waypoint")
def mark_waypoint(body: dict):
    if pose.latest is None:
        raise HTTPException(409, "No SLAM pose yet — is go2-slam receiving LiDAR data?")
    stale = (time.monotonic_ns() - pose.latest["received_ns"]) / 1e9
    if stale > 2.0:
        raise HTTPException(409, f"SLAM pose is {stale:.1f}s stale — not marking a dead pose")
    wp = {
        "name": body.get("name", f"waypoint_{len(waypoints) + 1}"),
        "x": pose.latest["x"], "y": pose.latest["y"], "z": pose.latest["z"],
        "qx": pose.latest["qx"], "qy": pose.latest["qy"],
        "qz": pose.latest["qz"], "qw": pose.latest["qw"],
        "marked_at": time.strftime("%H:%M:%S"),
    }
    waypoints.append(wp)
    _persist_waypoints()
    return {"ok": True, "waypoint": wp, "total": len(waypoints)}


@app.get("/waypoints")
def list_waypoints():
    return {"waypoints": waypoints}


def _list_keyframes():
    if not os.path.isdir(KEYFRAME_DIR):
        return []
    return [f for f in os.listdir(KEYFRAME_DIR) if f.startswith("kf_") and f.endswith(".npz")]


@app.post("/refine")
def do_refine():
    """Offline loop-closure refine over the walk's keyframes → map_refined.pcd.

    Point-LIO has no loop closure, so a full-home walk drifts ('doubled walls').
    This corrects it from the keyframes go2-slam dropped along the way. It
    declines (ok=False) rather than guess if there aren't enough keyframes or no
    confident loop closures — you then just export the raw map. [#2]
    """
    if not _list_keyframes():
        raise HTTPException(409, "No keyframes yet — walk (and loop back) first")
    try:
        res = refiner.run_refine(KEYFRAME_DIR, REFINED_PCD)
    except Exception as e:  # refine is experimental — never take the console down
        raise HTTPException(500, f"refine failed: {e}")
    return res


@app.post("/export")
def do_export():
    # prefer the loop-closed map if refine produced one; fall back to raw
    use_refined = os.path.exists(REFINED_PCD)
    src = REFINED_PCD if use_refined else RAW_PCD
    if not os.path.exists(src):
        raise HTTPException(409, "No map snapshot yet — map for at least 30s first")
    session = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(EXPORT_DIR, session)
    try:
        meta = exporter.run_export(
            src_pcd=src,
            out_dir=out,
            slice_min=float(os.environ.get("SLICE_MIN_M", "0.15")),
            slice_max=float(os.environ.get("SLICE_MAX_M", "1.50")),
            resolution=float(os.environ.get("GRID_RESOLUTION_M", "0.05")),
            lidar_height=float(os.environ.get("LIDAR_HEIGHT_OFFSET_M", "0.40")),
            waypoints=waypoints,
            loop_closed=use_refined,
        )
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "session": session, "meta": meta, "loop_closed": use_refined,
            "download": f"/download/{session}.zip"}


@app.get("/exports")
def list_exports():
    if not os.path.isdir(EXPORT_DIR):
        return {"exports": []}
    out = []
    for s in sorted(os.listdir(EXPORT_DIR), reverse=True):
        d = os.path.join(EXPORT_DIR, s)
        if os.path.isdir(d):
            files = [
                {"name": f, "bytes": os.path.getsize(os.path.join(d, f))}
                for f in sorted(os.listdir(d))
            ]
            out.append({"session": s, "files": files, "zip": f"/download/{s}.zip"})
    return {"exports": out}


def _safe_session_dir(session: str) -> str:
    d = os.path.realpath(os.path.join(EXPORT_DIR, session))
    if not d.startswith(os.path.realpath(EXPORT_DIR)) or not os.path.isdir(d):
        raise HTTPException(404, "unknown export session")
    return d


@app.get("/download/{session}.zip")
def download_zip(session: str):
    d = _safe_session_dir(session)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in os.listdir(d):
            z.write(os.path.join(d, f), arcname=f"{session}/{f}")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="g1-map-{session}.zip"'},
    )


@app.get("/download/{session}/{filename}")
def download_file(session: str, filename: str):
    d = _safe_session_dir(session)
    path = os.path.realpath(os.path.join(d, filename))
    if not path.startswith(d) or not os.path.isfile(path):
        raise HTTPException(404, "no such file")
    return FileResponse(path, filename=filename)


# ------------------------------------------------------- rosbag downloads
# The recorder writes bags to the shared /data volume; expose them so the
# raw capture can be pulled to a laptop (bags aren't zipped — the .mcap is
# streamed from disk via FileResponse so multi-GB bags don't buffer in RAM).
@app.get("/bags")
def list_bags():
    if not os.path.isdir(BAG_DIR):
        return {"bags": []}
    out = []
    for s in sorted(os.listdir(BAG_DIR), reverse=True):
        d = os.path.join(BAG_DIR, s)
        if os.path.isdir(d):
            files = [{"name": f, "bytes": os.path.getsize(os.path.join(d, f)),
                      "url": f"/download-bag/{s}/{f}"}
                     for f in sorted(os.listdir(d))]
            out.append({"session": s, "files": files,
                        "bytes": sum(f["bytes"] for f in files)})
    return {"bags": out}


@app.get("/download-bag/{session}/{filename}")
def download_bag_file(session: str, filename: str):
    base = os.path.realpath(BAG_DIR)
    path = os.path.realpath(os.path.join(base, session, filename))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        raise HTTPException(404, "no such bag file")
    return FileResponse(path, filename=filename)


# ------------------------------------------------- no-go zone editor API
import numpy as np
from PIL import Image

import nogo as nogo_mod


def _load_grid(session: str):
    d = _safe_session_dir(session)
    meta = json.load(open(os.path.join(d, "map_meta.json")))
    with open(os.path.join(d, "map.pgm"), "rb") as f:
        assert f.readline().strip() == b"P5"
        w, h = map(int, f.readline().split())
        f.readline()
        img = np.frombuffer(f.read(), dtype=np.uint8).reshape(h, w)
    return d, meta, img


@app.get("/nogo", response_class=HTMLResponse)
def nogo_page():
    with open(os.path.join(STATIC, "nogo.html")) as f:
        return f.read()


@app.get("/exports/{session}/grid.png")
def grid_png(session: str):
    _, _, img = _load_grid(session)
    # readable colors for the editor: unknown gray, free white, occupied near-black
    rgb = np.zeros((*img.shape, 3), dtype=np.uint8)
    rgb[img == 205] = (40, 48, 66)
    rgb[img == 254] = (215, 226, 244)
    rgb[img == 0] = (11, 18, 32)
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/exports/{session}/meta")
def export_meta(session: str):
    d = _safe_session_dir(session)
    return json.load(open(os.path.join(d, "map_meta.json")))


@app.get("/exports/{session}/nogo")
def get_nogo(session: str):
    d = _safe_session_dir(session)
    p = os.path.join(d, "nogo.json")
    return json.load(open(p)) if os.path.exists(p) else {"polygons": []}


@app.post("/exports/{session}/nogo")
def set_nogo(session: str, body: dict):
    """Save polygons (map coords) and regenerate keepout.pgm/.yaml.

    The keepout pair ships inside the download zip automatically since the
    zip endpoint walks the whole session directory.
    """
    d = _safe_session_dir(session)
    polys = body.get("polygons", [])
    for p in polys:
        if len(p.get("points", [])) < 3:
            raise HTTPException(422, f"polygon '{p.get('name','?')}' needs >=3 points")
    with open(os.path.join(d, "nogo.json"), "w") as f:
        json.dump({"frame": "map_floor_aligned", "polygons": polys}, f, indent=2)
    meta = json.load(open(os.path.join(d, "map_meta.json")))
    g = meta["grid"]
    keep = nogo_mod.build_keepout(polys, g["width"], g["height"],
                                  g["resolution_m"], g["origin"])
    with open(os.path.join(d, "keepout.pgm"), "wb") as f:
        f.write(f"P5\n{keep.shape[1]} {keep.shape[0]}\n255\n".encode())
        f.write(keep.tobytes())
    nogo_mod.write_keepout_yaml(os.path.join(d, "keepout.yaml"),
                                g["resolution_m"], g["origin"])
    forbidden_m2 = float((keep == 0).sum()) * g["resolution_m"] ** 2
    return {"ok": True, "polygons": len(polys), "forbidden_m2": round(forbidden_m2, 2)}
