"""go2-initial-test dashboard.

Fans out to each per-interface test service (FastAPI apps on localhost), merges
their `results[]`, and serves a single go/no-go board. All services run with
`network: host`, so they're reachable on 127.0.0.1:<port>.

Contract each test service implements:
  GET  /status -> { "results": [ {interface, status, detail, data}, ... ] }
  POST /run    -> re-run the test
status ∈ pass | fail | pending | manual | na
"""
import asyncio
import os

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

PORT = int(os.environ.get("PORT", "3600"))

# Test services (container -> localhost URL). Add an entry as each test ships.
SERVICES = {
    "gpu": "http://127.0.0.1:3610",
    "lowstate": "http://127.0.0.1:3611",
    "camera": "http://127.0.0.1:3612",
    "lidar": "http://127.0.0.1:3613",
    "mic": "http://127.0.0.1:3614",
    "speaker": "http://127.0.0.1:3615",
    "motion": "http://127.0.0.1:3616",
    "cloud": "http://127.0.0.1:3617",
    "extras": "http://127.0.0.1:3618",
    "storage": "http://127.0.0.1:3619",
    "vui": "http://127.0.0.1:3620",
    "bt": "http://127.0.0.1:3621",
    "btscan": "http://127.0.0.1:3622",
}

# The full checklist. Each interface is hosted by one service (above). `manual`
# marks tests that must be triggered by a human; `risk` flags unverified ones.
INTERFACES = [
    {"key": "camera",        "label": "RGB Camera",     "group": "Sensors",      "service": "camera"},
    {"key": "lidar",         "label": "LiDAR",          "group": "Sensors",      "service": "lidar"},
    {"key": "imu",           "label": "IMU",            "group": "Sensors",      "service": "lowstate"},
    {"key": "foot_contact",  "label": "Foot contact",   "group": "Sensors",      "service": "lowstate"},
    {"key": "ultrasonic",    "label": "Ultrasonic",     "group": "Sensors",      "service": "extras", "risk": "No known API — unverified"},
    {"key": "battery",       "label": "Battery",        "group": "Power",        "service": "lowstate"},
    {"key": "microphone",    "label": "Microphone",     "group": "Audio",        "service": "mic"},
    {"key": "speaker",       "label": "Speaker",        "group": "Audio",        "service": "speaker", "manual": True},
    {"key": "motion",        "label": "Motion (walk)",  "group": "Actuation",    "service": "motion",  "manual": True},
    {"key": "posture",       "label": "Posture / gait", "group": "Actuation",    "service": "motion",  "manual": True},
    {"key": "obstacle_avoid","label": "Obstacle avoid", "group": "Actuation",    "service": "motion",  "manual": True},
    {"key": "acrobatics",    "label": "Acrobatics",     "group": "Actuation",    "service": "motion",  "manual": True, "danger": True},
    {"key": "body_leds",     "label": "VUI (light/vol)","group": "Actuation",    "service": "vui",     "manual": True},
    {"key": "camera_gimbal", "label": "Camera gimbal",  "group": "Actuation",    "service": "extras"},
    {"key": "joints",        "label": "Joints / motors","group": "Robot state",  "service": "lowstate"},
    {"key": "odometry",      "label": "Odometry",       "group": "Robot state",  "service": "lowstate"},
    {"key": "remote",        "label": "Wireless remote","group": "Robot state",  "service": "lowstate"},
    {"key": "uwb",           "label": "UWB tag",        "group": "Robot state",  "service": "lowstate", "risk": "Optional paid accessory — na if not fitted"},
    {"key": "gpu",           "label": "GPU / CUDA",     "group": "Compute",      "service": "gpu"},
    {"key": "jetson",        "label": "Jetson (cores/TRT)","group": "Compute",   "service": "gpu"},
    {"key": "storage",       "label": "Storage",        "group": "Compute",      "service": "storage"},
    {"key": "internet",      "label": "Internet",       "group": "Connectivity", "service": "cloud"},
    {"key": "cloud",         "label": "Wendy Cloud",    "group": "Connectivity", "service": "cloud"},
    {"key": "bluetooth",     "label": "Bluetooth (radio)","group": "Connectivity","service": "bt"},
    {"key": "bt_scan",       "label": "BT scan/pair",   "group": "Connectivity", "service": "btscan", "risk": "Exercises the bluetooth entitlement — pending here = entitlement/dbus-proxy issue"},
]

app = FastAPI(title="go2-initial-test")


async def _fetch(client: httpx.AsyncClient, name: str, url: str):
    try:
        r = await client.get(f"{url}/status", timeout=2.5)
        r.raise_for_status()
        return name, r.json().get("results", []), True
    except Exception:  # noqa: BLE001 — any failure = service not reachable yet
        return name, [], False


@app.get("/api/status")
async def api_status():
    """Poll every test service concurrently and overlay results on the checklist."""
    async with httpx.AsyncClient() as client:
        fetched = await asyncio.gather(*[_fetch(client, n, u) for n, u in SERVICES.items()])

    by_key, up = {}, {}
    for name, results, ok in fetched:
        up[name] = ok
        for res in results:
            if isinstance(res, dict) and res.get("interface"):
                by_key[res["interface"]] = res

    tiles, summary = [], {"pass": 0, "fail": 0, "pending": 0, "manual": 0, "na": 0}
    for spec in INTERFACES:
        res = by_key.get(spec["key"])
        status = (res or {}).get("status") if res else None
        if not status:
            status = "pending"  # service down or hasn't reported this interface yet
        summary[status] = summary.get(status, 0) + 1
        tiles.append({
            **spec,
            "status": status,
            "detail": (res or {}).get("detail", "" if res else "service not deployed/reachable"),
            "data": (res or {}).get("data", {}),
        })

    # E2: report automated health separately from manual-trigger tests, so a
    # healthy robot greens without an operator pressing every manual button.
    auto = [t for t in tiles if not t.get("manual") and t["status"] != "na"]
    man = [t for t in tiles if t.get("manual")]
    af = sum(1 for t in auto if t["status"] == "fail")
    ap = sum(1 for t in auto if t["status"] == "pending")
    overall = "fail" if af else ("pending" if ap else "pass")  # automated verdict
    automated = {"pass": sum(1 for t in auto if t["status"] == "pass"),
                 "fail": af, "pending": ap, "total": len(auto)}
    manual = {"pending": sum(1 for t in man if t["status"] in ("manual", "pending")),
              "total": len(man)}
    return {"tiles": tiles, "summary": summary, "overall": overall,
            "automated": automated, "manual": manual, "services_up": up}


@app.post("/api/run/{key}")
async def api_run(key: str):
    """Trigger a re-run / manual test by posting to the hosting service's /run."""
    spec = next((i for i in INTERFACES if i["key"] == key), None)
    if not spec:
        return JSONResponse({"ok": False, "error": "unknown interface"}, status_code=404)
    url = SERVICES.get(spec["service"])
    try:
        async with httpx.AsyncClient() as client:
            # Manual tests can run long (motion posture/acrobatics; speaker is now
            # fire-and-forget) — give the trigger headroom so it doesn't 502 a run
            # that's actually working.
            r = await client.post(f"{url}/run", json={"interface": key}, timeout=60)
            return {"ok": r.status_code < 400, "status_code": r.status_code, "body": r.text[:500]}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
