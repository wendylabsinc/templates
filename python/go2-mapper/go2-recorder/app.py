#!/usr/bin/env python3
"""
go2-recorder — rosbag2 insurance recording, controlled over HTTP.

Runs alongside live SLAM so a bad map never costs you the walk: re-run
SLAM offline from the bag with tweaked params instead of re-walking.

  POST /start  -> begins `ros2 bag record` of RECORD_TOPICS into
                  $BAG_DIR/<session-id>/ ; returns {"session": ...}
  POST /stop   -> SIGINT the recorder (rosbag2 finalizes metadata on SIGINT)
  GET  /status -> {"recording": bool, "session": str|None, "size_bytes": int}
"""

import os
import shutil
import signal
import subprocess
import time

from fastapi import FastAPI

BAG_DIR = os.environ.get("BAG_DIR", "/data/bags")
TOPICS = os.environ.get("RECORD_TOPICS", "/utlidar/cloud_deskewed /utlidar/imu").split()

app = FastAPI()
state = {"proc": None, "session": None}


def _dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        total += sum(os.path.getsize(os.path.join(root, f)) for f in files)
    return total


@app.post("/start")
def start():
    if state["proc"] and state["proc"].poll() is None:
        return {"ok": False, "error": "already recording", "session": state["session"]}
    session = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(BAG_DIR, session)
    os.makedirs(BAG_DIR, exist_ok=True)
    cmd = ["ros2", "bag", "record", "-o", out, *TOPICS]
    state["proc"] = subprocess.Popen(cmd)
    state["session"] = session
    return {"ok": True, "session": session, "topics": TOPICS}


@app.post("/stop")
def stop():
    p = state["proc"]
    if not p or p.poll() is not None:
        return {"ok": False, "error": "not recording"}
    p.send_signal(signal.SIGINT)  # lets rosbag2 write metadata.yaml cleanly
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill()
    return {"ok": True, "session": state["session"]}


@app.get("/status")
def status():
    recording = bool(state["proc"] and state["proc"].poll() is None)
    size = 0
    if state["session"]:
        path = os.path.join(BAG_DIR, state["session"])
        if os.path.isdir(path):
            size = _dir_size(path)
    disk = shutil.disk_usage(BAG_DIR if os.path.isdir(BAG_DIR) else "/")
    return {
        "recording": recording,
        "session": state["session"],
        "size_bytes": size,
        "disk_free_bytes": disk.free,
    }
