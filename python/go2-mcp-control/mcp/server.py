#!/usr/bin/env python3
"""mcp_server -- a FastMCP (HTTP) server that exposes the REAL Go2's motion
surface as Claude tools, so you can drive the physical dog in natural language.

It is a thin bridge: one MCP tool per go2-motion HTTP endpoint. go2-motion
(the sibling service) is the only process that links the Unitree SDK; it holds
the SportClient, the velocity caps, and the watchdog. This server just turns a
tool call into an HTTP request to `MOTION_URL` (default 127.0.0.1:3201 — the
sibling container on the dog's host network) and returns the JSON.

SCOPE: the physical dog's own motion/gesture repertoire (stand, sit, walk,
turn, gestures, low-level velocity, telemetry). The dog walks itself and does
not autonomously map/navigate, so there are deliberately no explore/inspect/zone
tools here — just the motion surface go2-motion exposes.

Transport: streamable HTTP (FastMCP 2.x), served on 0.0.0.0:$MCP_PORT at the
`/mcp` path. Register with the Claude CLI:
    claude mcp add --transport http go2 http://<dog-ip>:<MCP_PORT>/mcp

If go2-motion is not up yet, every tool returns a clear {"success": false,
"message": ...} instead of crashing, so Claude can explain the state and retry.
"""

import os

import httpx
from fastmcp import FastMCP

mcp = FastMCP("go2-motion-control")

# The sibling go2-motion service. Default assumes host networking on the dog
# (both containers on 127.0.0.1); override MOTION_URL to point at a remote dog.
MOTION_URL = os.environ.get("MOTION_URL", "http://127.0.0.1:3201").rstrip("/")
MCP_PORT = int(os.environ.get("MCP_PORT", "3450"))
# Skills (stand/sit/dance/hello) trigger a mode switch on the dog and can take
# a few seconds; a blocking /move sleeps for its full duration. Keep the HTTP
# timeout comfortably above the motion side's 10 s duration cap.
HTTP_TIMEOUT_S = float(os.environ.get("MCP_HTTP_TIMEOUT_S", "15.0"))

# Safety velocity presets for the named directions. These sit well inside
# go2-motion's caps (vx≤0.6, vy≤0.4, vyaw≤1.0), which re-clamp anyway — the
# hardware-side limits are the real guardrail, these are just gentle defaults.
_DIRECTIONS = {
    "forward":   {"vx": 0.3, "vy": 0.0, "vyaw": 0.0},
    "backward":  {"vx": -0.3, "vy": 0.0, "vyaw": 0.0},
    "left":      {"vx": 0.0, "vy": 0.3, "vyaw": 0.0},   # strafe left
    "right":     {"vx": 0.0, "vy": -0.3, "vyaw": 0.0},  # strafe right
    "turn_left": {"vx": 0.0, "vy": 0.0, "vyaw": 0.6},   # rotate CCW in place
    "turn_right": {"vx": 0.0, "vy": 0.0, "vyaw": -0.6},  # rotate CW in place
}

# Natural-language synonyms -> canonical direction. Lets Claude pass whatever
# the user said ("back up", "spin left", "strafe right") and still hit a known
# preset; anything unrecognised is reported back with the valid options.
_DIRECTION_SYNONYMS = {
    "forward": "forward", "forwards": "forward", "ahead": "forward", "fwd": "forward",
    "backward": "backward", "backwards": "backward", "back": "backward",
    "reverse": "backward", "backup": "backward", "back_up": "backward",
    "left": "left", "strafe_left": "left", "sidestep_left": "left",
    "right": "right", "strafe_right": "right", "sidestep_right": "right",
    "turn_left": "turn_left", "turnleft": "turn_left", "spin_left": "turn_left",
    "rotate_left": "turn_left", "left_turn": "turn_left", "ccw": "turn_left",
    "turn_right": "turn_right", "turnright": "turn_right", "spin_right": "turn_right",
    "rotate_right": "turn_right", "right_turn": "turn_right", "cw": "turn_right",
}


def _norm_direction(direction: str) -> str | None:
    """Map a free-form direction word to a canonical preset key, or None.

    Normalises case/spacing/hyphens ('Turn Left' / 'turn-left' -> 'turn_left')
    then resolves synonyms. Returns None if it isn't a movement direction we
    know, so the caller can reply with the valid set (lets Claude recover)."""
    key = (direction or "").strip().lower().replace(" ", "_").replace("-", "_")
    return _DIRECTION_SYNONYMS.get(key)


def _post(path: str, json: dict | None = None) -> dict:
    """POST to go2-motion and return a JSON-able dict.

    Network/HTTP errors are masked into {"success": false, "message": ...} —
    matching the source MCP server's philosophy that a tool never raises, it
    reports, so the model can explain the failure and decide what to do next."""
    try:
        r = httpx.post(f"{MOTION_URL}{path}", json=json, timeout=HTTP_TIMEOUT_S)
    except httpx.HTTPError as exc:
        return {
            "success": False,
            "message": (
                f"go2-motion unreachable at {MOTION_URL}{path} ({type(exc).__name__}: {exc}). "
                f"Is the 'motion' service running on the dog and reachable at MOTION_URL?"
            ),
        }
    if r.status_code >= 400:
        return {"success": False, "message": f"{path} -> HTTP {r.status_code}: {r.text}"}
    try:
        return {"success": True, "result": r.json()}
    except ValueError:
        return {"success": True, "result": r.text}


def _get(path: str) -> dict:
    try:
        r = httpx.get(f"{MOTION_URL}{path}", timeout=HTTP_TIMEOUT_S)
    except httpx.HTTPError as exc:
        return {
            "success": False,
            "message": (
                f"go2-motion unreachable at {MOTION_URL}{path} ({type(exc).__name__}: {exc}). "
                f"Is the 'motion' service running on the dog and reachable at MOTION_URL?"
            ),
        }
    if r.status_code >= 400:
        return {"success": False, "message": f"{path} -> HTTP {r.status_code}: {r.text}"}
    try:
        return {"success": True, "result": r.json()}
    except ValueError:
        return {"success": True, "result": r.text}


# ---------------------------------------------------------------- POSTURE / GESTURES
@mcp.tool
def stand_up() -> dict:
    """Make the Go2 stand up on all four legs (its normal ready posture). Call
    this before walking if the dog is sitting or lying down."""
    return _post("/stand")


@mcp.tool
def sit_down() -> dict:
    """Make the Go2 sit down. A resting posture; the dog must stand_up again
    before it can walk."""
    return _post("/sit")


@mcp.tool
def lie_down() -> dict:
    """Make the Go2 lie down (lowest posture, motors relaxed). The dog must
    stand_up again before it can walk."""
    return _post("/lie")


@mcp.tool
def wave_hello() -> dict:
    """Make the Go2 perform its 'hello' wave gesture (lifts a front paw). A
    friendly greeting; the dog returns to standing afterwards."""
    return _post("/hello")


@mcp.tool
def dance() -> dict:
    """Make the Go2 perform its built-in dance routine. Fun demo move; keep a
    clear area around the dog. Returns to standing afterwards."""
    return _post("/dance")


# ---------------------------------------------------------------- MOTION
@mcp.tool
def move(direction: str, seconds: float = 2.0) -> dict:
    """Walk the Go2 in a DIRECTION for a fixed time, then stop automatically.

    direction: one of forward, backward, left, right, turn_left, turn_right
      (common synonyms like 'back', 'strafe left', 'spin right' also work).
      left/right STRAFE sideways; turn_left/turn_right ROTATE in place.
    seconds: how long to move, clamped to 0.1–10 s.

    This is a blocking, self-terminating move: the dog walks at a safe preset
    speed for `seconds`, then the motion service issues StopMove(). Velocities
    are re-clamped to the hardware caps on the motion side."""
    key = _norm_direction(direction)
    if key is None:
        return {
            "success": False,
            "message": (
                f"unknown direction '{direction}'. Valid: "
                f"{', '.join(sorted(_DIRECTIONS))} (plus common synonyms)."
            ),
        }
    seconds = max(0.1, min(float(seconds), 10.0))
    body = dict(_DIRECTIONS[key])
    body["duration"] = seconds
    return _post("/move", body)


@mcp.tool
def set_velocity(vx: float = 0.0, vy: float = 0.0, vyaw: float = 0.0) -> dict:
    """Low-level, NON-BLOCKING velocity command (advanced).

    vx: forward speed m/s (+ forward), vy: strafe m/s (+ left),
    vyaw: yaw rate rad/s (+ counter-clockwise). All re-clamped on the motion
    side to vx≤0.6, vy≤0.4, vyaw≤1.0.

    Returns immediately and the dog keeps moving, but a 1 s watchdog on the
    motion service stops it automatically unless you send another
    set_velocity — so it will NOT run away if you stop calling. Prefer `move`
    for simple 'walk forward for N seconds' requests; use this for continuous
    closed-loop control."""
    return _post("/velocity", {"vx": vx, "vy": vy, "vyaw": vyaw})


@mcp.tool
def stop() -> dict:
    """STOP the Go2 immediately: halt all motion and cancel any pending
    watchdog. Call this whenever the user says stop/halt/freeze, or to clear a
    stuck movement. Safe to call when the dog is already idle."""
    return _post("/stop")


# ---------------------------------------------------------------- TELEMETRY
@mcp.tool
def get_state() -> dict:
    """Read the Go2's live telemetry from rt/lowstate: battery state-of-charge
    (%), power voltage, IMU roll/pitch/yaw, per-foot contact forces, and the
    controller tick. Read-only; safe to call anytime."""
    return _get("/state")


@mcp.tool
def get_health() -> dict:
    """Check whether the motion service is up and the SportClient is connected
    to the dog. Returns ok=true when the dog is ready to accept commands.
    Read-only; call this first if other tools report 'unreachable'."""
    return _get("/health")


def _run_http() -> None:
    """Serve over streamable HTTP on 0.0.0.0:$MCP_PORT at /mcp.

    FastMCP 2.x/3.x use transport="http" (the streamable-HTTP transport);
    older 2.x builds named it "streamable-http". Try the modern name first and
    fall back so the template works across the fastmcp we pin and any nearby
    version, without changing the client-facing /mcp URL."""
    for transport in ("http", "streamable-http"):
        try:
            mcp.run(transport=transport, host="0.0.0.0", port=MCP_PORT)
            return
        except (ValueError, TypeError) as exc:
            # ValueError: transport name not recognised by this fastmcp.
            # TypeError: this version doesn't accept host/port kwargs here.
            last = exc
            continue
    raise RuntimeError(f"no supported FastMCP HTTP transport found: {last}")


if __name__ == "__main__":
    _run_http()
