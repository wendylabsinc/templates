#!/usr/bin/env python3
"""Central component — the camera-wall dashboard.

This is the "central" tier of the fleet (see ../wendy.json `dashboard` component).
It does NOT know how many cameras exist or where they are. The platform resolves
the `camera` component's live endpoints (every device in the named group) and hands
them to us two ways — the contract delivered by WDY-1755:

  1. WENDY_FLEET_PEERS  — env var (or file path), a JSON snapshot injected at start:
         [{"name": "...", "url": "http://...:8000", "group": "cameras", "status": "ready"}, ...]
     The `url` is already reachable from here regardless of where this component runs
     (LAN-direct when co-located, auto-provisioned tunnel when remote/cloud).

  2. WENDY_DISCOVERY_URL — a local discovery API for LIVE membership: GET returns the
     current peer list as JSON. We poll it so cameras that join/leave/reboot show up
     without a redeploy. (A streaming/subscribe variant is a future nicety.)

Everything below is stdlib only. Run on a device/cloud via the Dockerfile, or
host-side with `python3 serve.py`.

  GET /             the dashboard UI
  GET /api/peers    current discovered cameras (the UI renders tiles from this)
"""
import json
import os
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("DASHBOARD_PORT", os.environ.get("PORT", "9000")))
DISCOVERY_URL = os.environ.get("WENDY_DISCOVERY_URL", "")
POLL_SECONDS = float(os.environ.get("FLEET_POLL_SECONDS", "3"))

_peers = []           # current list of {name, url, group, status}
_peers_lock = threading.Lock()


def _load_seed():
    """Initial peers from WENDY_FLEET_PEERS — either inline JSON or a file path."""
    raw = os.environ.get("WENDY_FLEET_PEERS", "").strip()
    if not raw:
        return []
    try:
        if os.path.isfile(raw):
            with open(raw) as f:
                raw = f.read()
        data = json.loads(raw)
        return data.get("peers", data) if isinstance(data, dict) else data
    except Exception as e:  # noqa: BLE001
        print(f"[fleet] could not parse WENDY_FLEET_PEERS: {e}")
        return []


def _poll_discovery():
    """Keep _peers in sync with the live discovery API (dynamic membership)."""
    while True:
        try:
            with urllib.request.urlopen(DISCOVERY_URL, timeout=4) as r:
                data = json.loads(r.read().decode())
            peers = data.get("peers", data) if isinstance(data, dict) else data
            if isinstance(peers, list):
                with _peers_lock:
                    _peers[:] = peers
        except Exception as e:  # noqa: BLE001
            print(f"[fleet] discovery poll failed: {e}")
        time.sleep(POLL_SECONDS)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=ROOT, **k)

    def do_GET(self):
        if self.path.startswith("/api/peers"):
            with _peers_lock:
                body = json.dumps({"peers": list(_peers)}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    with _peers_lock:
        _peers[:] = _load_seed()
    print(f"[fleet] seeded {len(_peers)} camera(s) from WENDY_FLEET_PEERS")
    if DISCOVERY_URL:
        threading.Thread(target=_poll_discovery, daemon=True).start()
        print(f"[fleet] polling live discovery: {DISCOVERY_URL} every {POLL_SECONDS}s")
    else:
        print("[fleet] WENDY_DISCOVERY_URL unset — using the static seed only")
    print(f"camera-wall dashboard on http://0.0.0.0:{PORT}/")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
