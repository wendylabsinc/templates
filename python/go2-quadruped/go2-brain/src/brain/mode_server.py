"""Tiny stdlib HTTP server: controller mode + live param tuning + UI.

Endpoints (all on BRAIN_MODE_PORT, default 3300):

    GET  /                   → operator UI (HTML page; sliders + STOP)
    GET  /health             → liveness probe
    GET  /mode               → current mode name + available list
    POST /mode/<name>        → switch to <name>; returns new name
    POST /stop               → emergency: switch to mock + stop dog
    GET  /params             → {params, defaults, bounds}
    POST /params             → bulk update (JSON body); returns new params
    POST /params/reset       → reload params from env (startup defaults)

Examples (from your Mac):

    curl http://192.168.123.18:3300/mode
    curl -X POST http://192.168.123.18:3300/mode/unitree
    curl http://192.168.123.18:3300/params
    curl -X POST http://192.168.123.18:3300/params \\
         -H content-type:application/json -d '{"k_yaw": 1.5, "max_vx": 0.5}'

Or just open `http://192.168.123.18:3300/` in a browser / phone.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import os

from . import fusion as _fusion
from . import trail as _trail
from .dog.switchable import SwitchableController
from .perception import (
    DECISION_STALE_S,
    FREE_SPACE_STALE_S,
    POSE_STALE_S,
    VISION_STALE_S,
)
from .state_machine import PARAM_BOUNDS, Params, ParamStore

logger = logging.getLogger(__name__)

# The tuning UI is a single self-contained HTML file shipped next to this
# module. Loaded lazily on the first GET / so missing-file breakage is
# visible at request time (and so we pick up edits without restarting in
# dev, since this is localhost and the file is small).
_UI_PATH = Path(__file__).parent / "static" / "index.html"


def _bounds_payload() -> dict:
    return {
        name: {"min": lo, "max": hi, "step": step, "label": label, "info": info}
        for name, (lo, hi, step, label, info) in PARAM_BOUNDS.items()
    }


def _perception_params_readonly() -> dict:
    """Read-only snapshot of the env-var-driven fusion/trail/safety/
    staleness knobs. Surfaced in /params so operators can see what's set
    without SSHing into the container; live tuning of these stays env-only
    for now (would require those modules to take a shared param store)."""
    return {
        "staleness_s": {
            "decision": DECISION_STALE_S,
            "vision": VISION_STALE_S,
            "pose": POSE_STALE_S,
            "free_space": FREE_SPACE_STALE_S,
        },
        "fusion": {
            "fov_half_deg": _fusion.FOV_HALF_DEG,
            "match_bearing_deg": _fusion.MATCH_BEARING_DEG,
            "outlier_gate_deg": _fusion.OUTLIER_GATE_DEG,
            "vision_decay_s": _fusion.VISION_DECAY_S,
            "track_drop_after_s": _fusion.TRACK_DROP_AFTER_S,
            "followable_confidence": _fusion.FOLLOWABLE_CONFIDENCE,
            "followable_max_age_s": _fusion.FOLLOWABLE_MAX_AGE_S,
            "min_track_age_frames": _fusion.MIN_TRACK_AGE_FRAMES,
        },
        "trail": {
            "history_s": _trail.TRAIL_HISTORY_S,
            "max_entries": _trail.TRAIL_MAX_ENTRIES,
            "approach_s": _trail.TRAIL_APPROACH_S,
            "extrapolate_s": _trail.TRAIL_EXTRAPOLATE_S,
            "tangent_window_s": _trail.TANGENT_WINDOW_S,
            "max_tangent_mps": _trail.MAX_TANGENT_MPS,
        },
        "safety": {
            "min_distance_m": float(
                os.environ.get("BRAIN_SAFETY_MIN_DISTANCE_M", "0.40")
            ),
            "ramp_m": float(os.environ.get("BRAIN_SAFETY_RAMP_M", "0.30")),
            "cone_half_deg": float(
                os.environ.get("BRAIN_SAFETY_CONE_HALF_DEG", "30.0")
            ),
            "strict": os.environ.get("BRAIN_SAFETY_STRICT", "0") in ("1", "true", "True"),
        },
    }


class _ModeHandler(BaseHTTPRequestHandler):
    controller: SwitchableController = None  # type: ignore[assignment]
    param_store: ParamStore = None  # type: ignore[assignment]

    # Quiet the default per-request logging (HTTPServer prints to stderr
    # by default — too chatty for a tick loop's neighbour).
    def log_message(self, format, *args):  # noqa: A002, N802
        return

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Prevent stale UI when we ship updates — operator hits refresh,
        # gets the latest. The file is tiny so caching buys nothing.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> Optional[dict]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._json(400, {"error": f"bad JSON: {exc}"})
            return None
        if not isinstance(data, dict):
            self._json(400, {"error": "expected JSON object"})
            return None
        return data

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            try:
                body = _UI_PATH.read_bytes()
            except FileNotFoundError:
                self._json(500, {"error": f"UI not found at {_UI_PATH}"})
                return
            self._html(200, body)
            return
        if self.path == "/health":
            self._json(200, {"ok": True})
            return
        if self.path == "/mode":
            self._json(200, {
                "mode": self.controller.current_name,
                "available": self.controller.available,
            })
            return
        if self.path == "/params":
            current = self.param_store.snapshot()
            self._json(200, {
                "params": dataclasses.asdict(current),
                "defaults": dataclasses.asdict(Params()),
                "bounds": _bounds_payload(),
                # Path string (or None if persistence disabled) so the UI
                # can show "saved to <path>" — operators tend to want to
                # know exactly where their changes live.
                "persistence_path": (
                    str(self.param_store.path) if self.param_store.path else None
                ),
                # Read-only perception knobs (env-driven). Surfaced here
                # so operators don't need to ssh in to inspect them.
                "perception_params": _perception_params_readonly(),
            })
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        # POST /mode/<name>
        if self.path.startswith("/mode/"):
            name = self.path[len("/mode/"):].strip("/")
            try:
                new = self.controller.set_mode(name)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, {"mode": new})
            return
        # POST /stop — emergency: drop to mock and stop everything.
        if self.path == "/stop":
            self.controller.set_mode("mock")
            self.controller.stop()
            self._json(200, {"mode": "mock", "stopped": True})
            return
        # POST /params/reset
        if self.path == "/params/reset":
            new = self.param_store.reset()
            self._json(200, {"params": dataclasses.asdict(new)})
            return
        # POST /params — bulk update (JSON body)
        if self.path == "/params":
            body = self._read_body()
            if body is None:
                return  # _read_body already replied 400
            try:
                new = self.param_store.update(**body)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, {"params": dataclasses.asdict(new)})
            return
        self._json(404, {"error": "not found"})


def serve_in_thread(
    controller: SwitchableController,
    param_store: ParamStore,
    port: int = 3300,
) -> ThreadingHTTPServer:
    """Start the mode-switch + params HTTP server on a daemon thread.

    Uses ThreadingHTTPServer so a slow request handler can't block other
    requests — the operator UI polls /params every second, and a single
    blocking handler used to make the whole UI go unresponsive.

    Returns the server so the caller can `shutdown()` it on exit.
    """
    _ModeHandler.controller = controller
    _ModeHandler.param_store = param_store
    server = ThreadingHTTPServer(("0.0.0.0", port), _ModeHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="mode-server")
    t.start()
    logger.info("mode-server listening on :%d (UI at /, params at /params)", port)
    return server
