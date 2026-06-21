"""Web dashboard for the policy runner: live status + start/stop/e-stop buttons.

Runs with network:host, so it reaches the runner on 127.0.0.1:RUNNER_PORT.
"""

import os

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path

RUNNER = f"http://127.0.0.1:{os.environ.get('RUNNER_PORT', '3700')}"
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="rl-policy-runner-ui")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
async def status():
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{RUNNER}/status", timeout=2.5)
            return JSONResponse(r.json())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"state": "unreachable", "detail": str(exc)}, status_code=200)


@app.post("/api/{action}")
async def control(action: str):
    if action not in ("start", "stop", "estop"):
        return JSONResponse({"error": "unknown action"}, status_code=400)
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{RUNNER}/{action}", timeout=5.0)
            return JSONResponse(r.json())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=502)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
