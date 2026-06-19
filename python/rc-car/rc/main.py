"""Teleop web UI for the rc-car app group.

Serves the control page and proxies the sibling services so the browser only
talks to one origin:
  * /api/camera          -> camera  MJPEG stream  (127.0.0.1:8000/stream/color)
  * POST /api/drive,/stop -> motion  control plane (127.0.0.1:3201)

Runs with `network: host`, so siblings are reachable on localhost.
"""
import logging
import os
import sys
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
import uvicorn

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("rc")

PORT = int(os.environ.get("PORT", "3500"))
MOTION_URL = os.environ.get("MOTION_URL", "http://127.0.0.1:3201").rstrip("/")
CAMERA_URL = os.environ.get("CAMERA_URL", "http://127.0.0.1:8000/stream/color")

_app_dir = Path(__file__).parent
app = FastAPI(title="rc-car-rc")

# Single shared client for the (light, frequent) control calls. The camera
# MJPEG stream is intentionally NOT proxied here — the UI loads it directly
# from the camera service so the heavy stream never starves these endpoints.
_client = httpx.AsyncClient(timeout=0.5, limits=httpx.Limits(max_connections=10))


@app.on_event("shutdown")
async def _shutdown():
    await _client.aclose()


@app.get("/")
def index():
    return FileResponse(_app_dir / "static" / "index.html")


@app.post("/api/drive")
async def drive(req: Request):
    body = await req.json()
    try:
        r = await _client.post(f"{MOTION_URL}/drive", json=body)
        return JSONResponse(r.json(), status_code=r.status_code)
    except httpx.HTTPError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


@app.post("/api/stop")
async def stop():
    try:
        r = await _client.post(f"{MOTION_URL}/stop")
        return JSONResponse(r.json(), status_code=r.status_code)
    except httpx.HTTPError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


@app.get("/api/health")
async def health():
    try:
        r = await _client.get(f"{MOTION_URL}/health")
        return JSONResponse(r.json(), status_code=r.status_code)
    except httpx.HTTPError as e:
        return JSONResponse({"connected": False, "error": str(e)}, status_code=502)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
