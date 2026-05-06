#!/usr/bin/env python3
"""
Supervisor entrypoint for the go2-watchtower app.

Launches three child processes, prefixes each line of their stdout/stderr with
the child's name (so `[video_bridge] Traceback...` never gets buried in
foxglove_bridge's channel-advertise spam), and keeps the rest of the app
running if one child dies.
"""

import os
import signal
import subprocess
import sys
import threading
import time

PORT = os.environ.get("FOXGLOVE_PORT", "8765")

procs: list[tuple[str, subprocess.Popen]] = []
shutting_down = threading.Event()

# Children whose stdout we also tee to a file inside the container, so
# debugging the UWB pipeline doesn't require Foxglove or `wendy logs`
# scrolling — exec in and `tail -f /tmp/uwb.log`. The path is
# container-local: it survives child respawns but is wiped on a full
# container restart. If durability is needed across restarts, add a
# `persist` entitlement to wendy.json mounting /data and point the
# path there. Override at runtime with UWB_LOG_FILE=/some/other/path
# (set to empty string to disable).
UWB_LOG_FILE = os.environ.get("UWB_LOG_FILE", "/tmp/uwb.log")
LOG_FILES: dict[str, str] = (
    {"uwb_bridge": UWB_LOG_FILE, "uwb_filter": UWB_LOG_FILE}
    if UWB_LOG_FILE
    else {}
)


def _open_log(path: str | None):
    if not path:
        return None
    try:
        return open(path, "a", buffering=1)
    except OSError as e:
        print(f"[supervisor] could not open log file {path}: {e}", flush=True)
        return None


def _pump(name: str, stream) -> None:
    log_fp = _open_log(LOG_FILES.get(name))
    try:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            decorated = f"[{name}] {line}"
            print(decorated, flush=True)
            if log_fp is not None:
                # Wall-clock prefix so log lines can be cross-correlated
                # with sportmodestate / vision-track timestamps without
                # untangling rclpy's relative timestamps.
                log_fp.write(f"{time.strftime('%H:%M:%S')} {decorated}\n")
    finally:
        if log_fp is not None:
            try:
                log_fp.close()
            except OSError:
                pass
        stream.close()


def launch(name: str, cmd: list[str]) -> None:
    print(f"[supervisor] launching {name}: {' '.join(cmd)}", flush=True)
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    procs.append((name, p))
    cmds[name] = cmd
    threading.Thread(target=_pump, args=(name, p.stdout), daemon=True).start()


# Children we'll restart automatically on non-zero exit. video_bridge in
# particular dies cleanly (os._exit) when WebRTC drops, expecting to be
# respawned with a fresh handshake. Anything else dies → log once and
# leave it alone.
RESPAWN = {"video_bridge"}
# Backoff: max N restarts per window; after that, give up (sticky failure
# means something's actually wrong and we'd just spin).
RESTART_WINDOW_S = 60.0
RESTART_MAX_PER_WINDOW = 30
RESTART_BACKOFF_S = 1.0

# Sidecar storage so we can rebuild the same launch on respawn.
cmds: dict[str, list[str]] = {}
# (count_in_window, window_start_monotonic) per child name.
restart_state: dict[str, tuple[int, float]] = {}


def _maybe_respawn(name: str, idx: int) -> bool:
    """If `name` is restartable and within the cap, spawn a new instance
    and replace it in `procs[idx]`. Returns True iff a new process was
    started."""
    if name not in RESPAWN:
        return False
    cmd = cmds.get(name)
    if cmd is None:
        return False
    now = time.monotonic()
    count, window_start = restart_state.get(name, (0, now))
    if now - window_start > RESTART_WINDOW_S:
        count, window_start = 0, now
    if count >= RESTART_MAX_PER_WINDOW:
        print(
            f"[supervisor] {name} hit restart cap "
            f"({RESTART_MAX_PER_WINDOW}/{RESTART_WINDOW_S:.0f}s); "
            f"not respawning",
            flush=True,
        )
        return False
    time.sleep(RESTART_BACKOFF_S)
    print(
        f"[supervisor] respawning {name} (#{count + 1} this minute): "
        f"{' '.join(cmd)}",
        flush=True,
    )
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    procs[idx] = (name, p)
    restart_state[name] = (count + 1, window_start)
    threading.Thread(target=_pump, args=(name, p.stdout), daemon=True).start()
    return True


def _dump_topic_list() -> None:
    # Wait a bit longer than the foxglove_bridge bring-up so DDS discovery
    # has converged before we ask `ros2 topic list -t`.
    time.sleep(15)
    try:
        out = subprocess.check_output(
            ["ros2", "topic", "list", "-t"],
            stderr=subprocess.STDOUT,
            timeout=10,
        ).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[topics] ros2 topic list failed: {e}", flush=True)
        return
    print("[topics] ===== DDS topic inventory =====", flush=True)
    for line in out.strip().splitlines():
        print(f"[topics] {line}", flush=True)
    print("[topics] ===== end =====", flush=True)


def shutdown(*_):
    if shutting_down.is_set():
        return
    shutting_down.set()
    for name, p in procs:
        if p.poll() is None:
            print(f"[supervisor] terminating {name}", flush=True)
            p.terminate()
    deadline = time.time() + 5
    for _, p in procs:
        try:
            p.wait(timeout=max(0.0, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.kill()
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Exclude topics whose schema can't be resolved (unitree_interfaces /
    # unitree_arm packages aren't publicly available). The whitelist alone
    # isn't enough — foxglove_bridge's rosgraphPollThread still throws "bad
    # optional access" on every poll and stops doing dynamic topic discovery.
    # So we also start the bridges BEFORE foxglove_bridge: by the time it
    # does its initial scan, /go2/camera/compressed and /go2/mic/levels are
    # already on the graph and get advertised in the first pass.
    broken_topics = [
        # unitree_interfaces (package not public)
        "/query_result_node", "/query_result_edge",
        "/qt_command", "/qt_add_node", "/qt_add_edge",
        "/pctoimage_local",
        # unitree_arm (package not public)
        "/arm_Feedback", "/arm_Command",
        # unitree_go message types missing from the public unitree_go package
        "/config_change_status",
        "/utlidar/voxel_map_compressed",
    ]
    whitelist_regex = "^(?!" + "|".join(t + "$" for t in broken_topics) + ").*$"

    launch("video_bridge", ["python3", "-u", "/app/go2_video_bridge.py"])
    launch("mic_node", ["python3", "-u", "/app/go2_mic_node.py"])
    launch("audio_bridge", ["python3", "-u", "/app/go2_audio_bridge.py"])
    launch("uwb_bridge", ["python3", "-u", "/app/go2_uwb_bridge.py"])
    launch("uwb_filter", ["python3", "-u", "/app/go2_uwb_filter.py"])
    launch("dog_marker", ["python3", "-u", "/app/go2_dog_marker.py"])
    launch("vision_detector", ["python3", "-u", "/app/go2_vision_detector.py"])
    launch("vision_tracker", ["python3", "-u", "/app/go2_vision_tracker.py"])
    # Pose bridge: mirrors /sportmodestate as JSON on /go2/dog/pose_json
    # so go2-brain's ghost-trail recovery can anchor in world frame
    # without linking the Unitree IDL.
    launch("pose_bridge", ["python3", "-u", "/app/go2_pose_bridge.py"])
    # LIDAR filter: subscribes to /utlidar/cloud_deskewed (Livox MID-360
    # via Unitree's ROS2 driver), derives a 36-sector polar obstacle map
    # in base_link, publishes /go2/perception/free_space as JSON. Brain's
    # safety wrapper consumes this to clip vx/vy near walls.
    launch("lidar_filter", ["python3", "-u", "/app/go2_lidar_filter.py"])
    # TF bridge: republishes /utlidar/robot_pose as /tf (odom → base_link)
    # so Foxglove can render lidar points (in odom) alongside the dog
    # marker (in base_link) in one 3D panel without "Missing transform"
    # warnings.
    launch("tf_bridge", ["python3", "-u", "/app/go2_tf_bridge.py"])
    # Give the bridges a moment to register their publishers on the ROS graph
    # before foxglove_bridge enumerates topics.
    time.sleep(5)

    # Diagnostic: dump every DDS topic the container can see, with type info,
    # so we don't need ctr/docker exec to discover XT16 / L1 / aftermarket
    # sensor topics. One-shot, grep for it in the logs.
    threading.Thread(target=_dump_topic_list, daemon=True).start()

    launch("foxglove_bridge", [
        "ros2", "launch", "foxglove_bridge", "foxglove_bridge_launch.xml",
        f"port:={PORT}",
        f'topic_whitelist:=["{whitelist_regex}"]',
    ])

    # foxglove_bridge is the load-bearing one. If it dies, the dashboard is
    # gone and we let WendyOS restart the whole app. Children in RESPAWN
    # get auto-respawned (capped). Anything else dying → log once and
    # keep the rest of the app alive.
    reported: set[int] = set()  # indexed by id(Popen) so a respawn re-arms logging
    while not shutting_down.is_set():
        for i, (name, p) in enumerate(procs):
            rc = p.poll()
            if rc is None or id(p) in reported:
                continue
            reported.add(id(p))
            print(f"[supervisor] {name} exited with code {rc}", flush=True)
            if name == "foxglove_bridge":
                print("[supervisor] foxglove_bridge is required; shutting down", flush=True)
                shutdown()
                continue
            # Best-effort auto-respawn for transient bridges (WebRTC etc).
            _maybe_respawn(name, i)
        time.sleep(1)


if __name__ == "__main__":
    main()
