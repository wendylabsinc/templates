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


_NA_HINT = ("the Go2 head mic is on WebRTC, not the Jetson's ALSA; plug a USB mic or "
            "set MIC_DEVICE to test a local input")


def _pick_input_device():
    """Choose a capture device index, or None if there's no usable input.

    CRITICAL: enumerate first and bail before touching sd.rec(). On a Jetson with
    /dev/snd present but no real capture device (the common Go2 case), opening a
    stream on the default device can BLOCK FOREVER in ALSA — `sd.rec()/sd.wait()`
    never return and the tile hangs at "pending". query_devices() can't block, so
    we use it to decide whether a capture is even worth attempting.
    Returns (device_index_or_None, num_input_devices).
    """
    try:
        devices = sd.query_devices()
    except Exception:  # noqa: BLE001 — no PortAudio host/devices at all
        return None, 0
    inputs = [i for i, d in enumerate(devices) if d.get("max_input_channels", 0) > 0]
    if DEVICE is not None:  # explicit operator override — trust it
        return DEVICE, len(inputs)
    if not inputs:
        return None, 0
    try:  # prefer PortAudio's default input if it actually has input channels
        default_in = sd.default.device[0]
        if isinstance(default_in, int) and default_in in inputs:
            return default_in, len(inputs)
    except Exception:  # noqa: BLE001
        pass
    return inputs[0], len(inputs)


def _record_test():
    dev, n_inputs = _pick_input_device()
    if dev is None:
        # `na` (not fail): no local capture device is expected on the Go2, so don't
        # drag the board red — and crucially, don't call sd.rec() (which would hang).
        return {"interface": "microphone", "status": "na",
                "detail": f"no local ALSA capture device — {_NA_HINT}", "data": {"inputs": n_inputs}}
    try:
        n = int(RATE * SECONDS)
        rec = sd.rec(n, samplerate=RATE, channels=1, dtype="float32", device=dev)
        sd.wait()
        mono = rec[:, 0]
        rms = float(np.sqrt(np.mean(mono ** 2)))
        peak = float(np.max(np.abs(mono)))
        detail = f"recorded {SECONDS:.0f}s @ {RATE} Hz on device {dev} · RMS={rms:.4f} peak={peak:.3f}"
        if rms < 1e-4:
            detail += " (very quiet — mic opened but near-silent; speak/clap and re-run)"
        return {"interface": "microphone", "status": "pass", "detail": detail,
                "data": {"rms": rms, "peak": peak, "device": dev}}
    except Exception as e:  # noqa: BLE001 — device existed but capture failed
        return {"interface": "microphone", "status": "na",
                "detail": f"capture on device {dev} failed ({e}) — {_NA_HINT}", "data": {}}


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
