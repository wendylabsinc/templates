"""connectivity test — general internet + Wendy Cloud reachability.

Two interfaces from one service:
  internet -> can the container reach the public internet at all?
  cloud    -> can it reach Wendy Cloud (and, if CLOUD_PUSH_URL set, ingest a POST)?
"""
import asyncio
import os
import time

import httpx
import uvicorn
from fastapi import FastAPI

PORT = int(os.environ.get("PORT", "3617"))
# Injected via the CLOUD_HEALTH_URL template var (set as an ENV in the Dockerfile);
# no hostname baked into source here.
HEALTH_URL = os.environ.get("CLOUD_HEALTH_URL", "")
PUSH_URL = os.environ.get("CLOUD_PUSH_URL", "")
# A lightweight public endpoint that returns 204 — good for a raw connectivity check.
INTERNET_URL = os.environ.get("INTERNET_URL", "https://www.google.com/generate_204")

app = FastAPI(title="go2-test-cloud")
_results = {
    "internet": {"interface": "internet", "status": "pending", "detail": "not run yet", "data": {}},
    "cloud": {"interface": "cloud", "status": "pending", "detail": "not run yet", "data": {}},
}


async def _test_internet(c):
    t0 = time.time()
    try:
        r = await c.get(INTERNET_URL)
        ms = round((time.time() - t0) * 1000)
        ok = r.status_code < 400
        return {"interface": "internet", "status": "pass" if ok else "fail",
                "detail": f"{INTERNET_URL} → {r.status_code} ({ms} ms)", "data": {"latency_ms": ms}}
    except Exception as e:  # noqa: BLE001
        return {"interface": "internet", "status": "fail",
                "detail": f"no public internet: {e}", "data": {}}


async def _test_cloud(c):
    data = {}
    if not HEALTH_URL:
        return {"interface": "cloud", "status": "na",
                "detail": "CLOUD_HEALTH_URL not set — no Wendy Cloud endpoint to check", "data": {}}
    try:
        r = await c.get(HEALTH_URL)
        data["health_status"] = r.status_code
        if PUSH_URL:
            payload = {"source": "go2-initial-test", "ts": time.time(), "ping": "hardware-check"}
            pr = await c.post(PUSH_URL, json=payload)
            data["push_status"] = pr.status_code
            ok = pr.status_code < 400
            return {"interface": "cloud", "status": "pass" if ok else "fail",
                    "detail": f"push → {pr.status_code} · health → {r.status_code}", "data": data}
        # Wendy Cloud is a gRPC endpoint: a plain GET (no application/grpc content
        # type) gets 415, and 401/403 also mean "server is up, just not this request".
        # Treat those as reachable rather than printing a scary bare code.
        grpc_reachable = r.status_code in (415, 401, 403)
        ok = r.status_code < 500
        if grpc_reachable:
            detail = (f"reachable — Wendy Cloud gRPC endpoint responding "
                      f"({r.status_code} to a plain GET is expected)")
        else:
            detail = f"Wendy Cloud → {r.status_code}"
        detail += " · set CLOUD_PUSH_URL to test a real ingest POST"
        return {"interface": "cloud", "status": "pass" if ok else "fail", "detail": detail, "data": data}
    except Exception as e:  # noqa: BLE001
        return {"interface": "cloud", "status": "fail", "detail": f"cloud unreachable: {e}", "data": data}


async def _run():
    async with httpx.AsyncClient(timeout=8.0) as c:
        net, cloud = await asyncio.gather(_test_internet(c), _test_cloud(c))
    _results["internet"], _results["cloud"] = net, cloud


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_run())


@app.get("/status")
def status():
    return {"results": list(_results.values())}


@app.post("/run")
async def rerun():
    await _run()
    return {"ok": all(r["status"] == "pass" for r in _results.values()), "results": list(_results.values())}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
