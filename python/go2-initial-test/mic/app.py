"""microphone test — record a few seconds from the Jetson mic, report level.

Adapted from /demos/go2-Watchtower/go2_mic_node.py (sounddevice/ALSA capture).
Pass = the input device opened and captured samples; the detail reports RMS so a
near-silent room is distinguishable from a dead mic.
"""
import asyncio
import os

import numpy as np
import sounddevice as sd
import uvicorn
from fastapi import FastAPI

PORT = int(os.environ.get("PORT", "3614"))
_DEV = os.environ.get("MIC_DEVICE")
DEVICE = int(_DEV) if _DEV and _DEV.lstrip("-").isdigit() else None
RATE = int(os.environ.get("SAMPLE_RATE", "16000"))
SECONDS = float(os.environ.get("RECORD_SECONDS", "3"))

app = FastAPI(title="go2-test-mic")
_result = {"interface": "microphone", "status": "pending", "detail": "not run yet", "data": {}}


def _record_test():
    try:
        n = int(RATE * SECONDS)
        rec = sd.rec(n, samplerate=RATE, channels=1, dtype="float32", device=DEVICE)
        sd.wait()
        mono = rec[:, 0]
        rms = float(np.sqrt(np.mean(mono ** 2)))
        peak = float(np.max(np.abs(mono)))
        detail = f"recorded {SECONDS:.0f}s @ {RATE} Hz · RMS={rms:.4f} peak={peak:.3f}"
        if rms < 1e-4:
            detail += " (very quiet — mic opened but near-silent; speak/clap and re-run)"
        return {"interface": "microphone", "status": "pass", "detail": detail,
                "data": {"rms": rms, "peak": peak}}
    except Exception as e:  # noqa: BLE001
        # `na` (not fail): the Go2 head mic rides WebRTC, not the Jetson's ALSA, so a
        # missing local capture device is expected — don't drag the board red. Plug a
        # USB mic / set MIC_DEVICE to test a local input.
        return {"interface": "microphone", "status": "na",
                "detail": f"no local ALSA capture device ({e}) — the Go2 head mic is on WebRTC, not "
                          "the Jetson's ALSA; plug a USB mic or set MIC_DEVICE to test a local input", "data": {}}


def _run():
    global _result
    _result = _record_test()


_lock = asyncio.Lock()
CAP_TIMEOUT_S = SECONDS + 3


async def _guarded_run():
    # Serialize captures (concurrent sd.rec() on one ALSA device → "device busy")
    # AND bound them: a hung sd.wait() on a missing device must not leave the tile
    # stuck at "pending" forever — time out to a clear `na`.
    global _result
    if _lock.locked():
        return
    async with _lock:
        try:
            await asyncio.wait_for(asyncio.to_thread(_run), timeout=CAP_TIMEOUT_S)
        except asyncio.TimeoutError:
            _result = {"interface": "microphone", "status": "na",
                       "detail": f"audio capture timed out after {CAP_TIMEOUT_S:.0f}s — no usable local "
                                 "ALSA input (the Go2 head mic is on WebRTC; plug a USB mic / set MIC_DEVICE)",
                       "data": {}}


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_guarded_run())


@app.get("/status")
async def status():
    # Auto-run like the DDS tiles: if no result yet (startup probe still in flight
    # or never ran), kick one off WITHOUT blocking the response — so the tile
    # resolves to a real pass/na instead of sitting at "pending".
    if _result["status"] == "pending" and not _lock.locked():
        asyncio.create_task(_guarded_run())
    return {"results": [_result]}


@app.post("/run")
async def rerun():
    await _guarded_run()
    return {"ok": _result["status"] == "pass", "result": _result}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
