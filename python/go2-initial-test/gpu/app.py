"""GPU + Jetson compute tests.

Two interfaces (dashboard POSTs /run with {"interface": <key>}):
  gpu    — torch + CUDA visibility + a real GPU matmul timing
  jetson — deep dive: online CPU cores, TensorRT, nvpmodel power mode, tegrastats
"""
import asyncio
import os
import shutil
import subprocess
import time

import uvicorn
from fastapi import FastAPI, Request

PORT = int(os.environ.get("PORT", "3610"))
app = FastAPI(title="go2-test-gpu")

_results = {
    "gpu": {"interface": "gpu", "status": "pending", "detail": "not run yet", "data": {}},
    "jetson": {"interface": "jetson", "status": "pending", "detail": "not run yet", "data": {}},
}


def _test_gpu():
    try:
        import torch
    except Exception as e:  # noqa: BLE001
        return {"interface": "gpu", "status": "fail", "detail": f"torch import failed: {e}", "data": {}}
    data = {"torch": torch.__version__}
    if not torch.cuda.is_available():
        return {"interface": "gpu", "status": "fail",
                "detail": "torch.cuda.is_available() is False — no GPU visible (check the `gpu` "
                          "entitlement and that the image's CUDA matches the Go2's JetPack)", "data": data}
    try:
        name = torch.cuda.get_device_name(0)
        data.update(device=name, cuda=torch.version.cuda)
        x = torch.randn(2048, 2048, device="cuda")
        torch.cuda.synchronize()
        t0 = time.time()
        for _ in range(5):
            _ = x @ x
        torch.cuda.synchronize()
        data["matmul_ms"] = round((time.time() - t0) / 5 * 1000, 1)
        return {"interface": "gpu", "status": "pass",
                "detail": f"{name} · CUDA {data['cuda']} · 2k×2k matmul {data['matmul_ms']} ms", "data": data}
    except Exception as e:  # noqa: BLE001
        return {"interface": "gpu", "status": "fail", "detail": f"GPU op failed: {e}", "data": data}


def _test_jetson():
    data = {}
    try:
        data["online_cores"] = len(os.sched_getaffinity(0))
    except Exception:  # noqa: BLE001
        data["online_cores"] = os.cpu_count()
    trt_ok = False
    try:
        import tensorrt as trt
        data["tensorrt"] = trt.__version__
        trt_ok = True
    except Exception as e:  # noqa: BLE001
        data["tensorrt"] = f"unavailable ({type(e).__name__})"
    # nvpmodel + tegrastats are host JetPack tools; usually NOT inside a container
    # unless the host bin is mounted. Report honestly when absent.
    if shutil.which("nvpmodel"):
        try:
            out = subprocess.run(["nvpmodel", "-q"], capture_output=True, text=True, timeout=5).stdout.strip()
            data["nvpmodel"] = out.splitlines()[-1] if out else "?"
        except Exception as e:  # noqa: BLE001
            data["nvpmodel"] = f"err: {e}"
    else:
        data["nvpmodel"] = "not in container (host-only tool)"
    if shutil.which("tegrastats"):
        try:
            # tegrastats streams forever; let it print one sample then time out.
            p = subprocess.run(["tegrastats", "--interval", "1000"], capture_output=True, text=True, timeout=3)
            lines = (p.stdout or "").splitlines()
            data["tegrastats"] = lines[0] if lines else "no output"
        except subprocess.TimeoutExpired as e:
            out = (e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""))
            data["tegrastats"] = out.splitlines()[0] if out.splitlines() else "(no sample)"
        except Exception as e:  # noqa: BLE001
            data["tegrastats"] = f"err: {e}"
    else:
        data["tegrastats"] = "not in container (host-only tool)"
    cores = data.get("online_cores")
    detail = f"{cores} CPU cores online · TensorRT {data['tensorrt']} · power: {data['nvpmodel']}"
    # Don't hardcode pass: fail if we couldn't even read cores; `na` if the compute
    # env is up but TensorRT isn't present (informational, not a hardware fault);
    # pass only when cores read AND TensorRT is available.
    if not cores:
        return {"interface": "jetson", "status": "fail",
                "detail": "could not read CPU/compute info · " + detail, "data": data}
    if not trt_ok:
        return {"interface": "jetson", "status": "na",
                "detail": detail + " — TensorRT not present (informational)", "data": data}
    return {"interface": "jetson", "status": "pass", "detail": detail, "data": data}


_DISPATCH = {"gpu": _test_gpu, "jetson": _test_jetson}


def _run_all():
    for k, fn in _DISPATCH.items():
        _results[k] = fn()


@app.on_event("startup")
async def _startup():
    asyncio.create_task(asyncio.to_thread(_run_all))


@app.get("/status")
def status():
    return {"results": list(_results.values())}


@app.post("/run")
async def run(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    key = body.get("interface", "gpu")
    fn = _DISPATCH.get(key)
    if fn is None:
        return {"ok": False, "error": f"unknown interface {key}"}
    _results[key] = await asyncio.to_thread(fn)
    return {"ok": _results[key]["status"] == "pass", "result": _results[key]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
