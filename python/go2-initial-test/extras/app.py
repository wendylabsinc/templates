"""extras reporter — ultrasonic, body LEDs, camera gimbal.

None of these have a known WendyOS/SDK access path in the Go2 demos, so rather
than fake a green check we report them as `na` with the reason. They stay visible
on the board (not silently missing) and flagged as needing confirmation with
Unitree. If/when a topic or SDK method is found, turn the relevant entry into a
real probe.
"""
import os

import uvicorn
from fastapi import FastAPI

PORT = int(os.environ.get("PORT", "3618"))
app = FastAPI(title="go2-test-extras")


def _results():
    return [
        {"interface": "ultrasonic", "status": "na",
         "detail": "No DDS topic or SDK method found in the Go2 demos — unverified. "
                   "Confirm the DDS topic or I2C path with Unitree.", "data": {}},
        {"interface": "camera_gimbal", "status": "na",
         "detail": "Standard Go2 has a fixed forward-facing camera — no gimbal.", "data": {}},
    ]


@app.get("/status")
def status():
    return {"results": _results()}


@app.post("/run")
def rerun():
    return {"ok": False, "results": _results()}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
