"""Control API for the policy runner: start / stop / e-stop / status.

The UI (and you, via curl) drive the runner through these endpoints. The policy
never moves the robot until POST /start is called AND ENABLE_POLICY=1.
"""

import os

from fastapi import FastAPI
from controller import PolicyRunner

app = FastAPI(title="rl-policy-runner")
_runner = PolicyRunner()


@app.get("/status")
def status():
    return _runner.status()


@app.post("/start")
def start():
    return {"result": _runner.start()}


@app.post("/stop")
def stop():
    return {"result": _runner.stop()}


@app.post("/estop")
def estop():
    return {"result": _runner.estop()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "3700")))
