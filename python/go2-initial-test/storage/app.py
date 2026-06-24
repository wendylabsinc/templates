"""storage test — write+read a file on the persist volume, report free space."""
import asyncio
import os
import shutil
import time

import uvicorn
from fastapi import FastAPI

PORT = int(os.environ.get("PORT", "3619"))
DATA_DIR = os.environ.get("DATA_DIR", "/data")

app = FastAPI(title="go2-test-storage")
_result = {"interface": "storage", "status": "pending", "detail": "not run yet", "data": {}}


def _test():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        p = os.path.join(DATA_DIR, "go2-initial-test.probe")
        payload = f"go2-initial-test {time.time()}"
        with open(p, "w") as f:
            f.write(payload)
        with open(p) as f:
            got = f.read()
        if got != payload:
            return {"interface": "storage", "status": "fail",
                    "detail": f"readback mismatch in {DATA_DIR}", "data": {}}
        du = shutil.disk_usage(DATA_DIR)
        free_gb, total_gb = du.free / 1e9, du.total / 1e9
        return {"interface": "storage", "status": "pass",
                "detail": f"write/read OK at {DATA_DIR} · {free_gb:.1f}/{total_gb:.1f} GB free",
                "data": {"free_gb": round(free_gb, 1), "total_gb": round(total_gb, 1)}}
    except Exception as e:  # noqa: BLE001
        return {"interface": "storage", "status": "fail",
                "detail": f"storage error at {DATA_DIR}: {e} (is the persist entitlement mounted?)", "data": {}}


def _run():
    global _result
    _result = _test()


@app.on_event("startup")
async def _startup():
    await asyncio.to_thread(_run)


@app.get("/status")
def status():
    return {"results": [_result]}


@app.post("/run")
async def rerun():
    await asyncio.to_thread(_run)
    return {"ok": _result["status"] == "pass", "result": _result}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
