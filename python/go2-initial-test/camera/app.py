"""camera test — connect to the Go2's front camera over WebRTC, grab ONE frame.

The Go2 allows only ONE WebRTC client, so this connects, captures a single frame,
and immediately CLOSES the connection — it never holds the slot between tests.
`/run` makes a fresh attempt (so freeing the phone-app slot + re-running works).
Adapted from /demos/go2-camera/main.py.
"""
import asyncio
import os
import time

import uvicorn
from fastapi import FastAPI
from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod

GO2_IP = os.environ.get("GO2_IP", "192.168.123.161")
PORT = int(os.environ.get("PORT", "3612"))
TIMEOUT = float(os.environ.get("CAMERA_TIMEOUT_S", "15"))

app = FastAPI(title="go2-test-camera")
_result = {"interface": "camera", "status": "pending", "detail": "not run yet", "data": {}}
_lock = asyncio.Lock()


async def _capture_once():
    """Open WebRTC, return (w, h) of one decoded frame, always close the conn."""
    conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=GO2_IP)
    try:
        await conn.connect()
        holder = []  # list.append is GIL-safe even if the callback fires off-loop
        conn.video.switchVideoChannel(True)
        conn.video.add_track_callback(lambda t: holder.append(t))
        deadline = time.monotonic() + TIMEOUT  # ONE overall budget (track + decode)
        while not holder and time.monotonic() < deadline:  # wait for the track
            await asyncio.sleep(0.1)
        if not holder:
            raise TimeoutError("no video track offered by the Go2")
        track = holder[0]
        last = None
        while time.monotonic() < deadline:  # retry until a frame decodes
            try:
                frame = await asyncio.wait_for(track.recv(), timeout=max(0.5, deadline - time.monotonic()))
                img = frame.to_ndarray(format="bgr24")
                return img.shape[1], img.shape[0]
            except Exception as e:  # noqa: BLE001 — decoder warms up over a few frames
                last = e
                await asyncio.sleep(0.1)
        raise last or TimeoutError("no decodable frame within timeout")
    finally:
        try:
            await conn.close()  # release the single WebRTC slot
        except Exception:  # noqa: BLE001
            pass


def _run_capture():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_capture_once())
    finally:
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass


async def _do_test():
    global _result
    async with _lock:
        try:
            w, h = await asyncio.to_thread(_run_capture)
            _result = {"interface": "camera", "status": "pass",
                       "detail": f"{w}x{h} via WebRTC (1 frame captured, slot released)",
                       "data": {"width": w, "height": h}}
        except Exception as e:  # noqa: BLE001
            _result = {"interface": "camera", "status": "fail",
                       "detail": f"WebRTC capture failed: {e} — is the Go2 reachable at {GO2_IP} and "
                                 "the single WebRTC slot free (Unitree phone app closed)?",
                       "data": {}}


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_do_test())


@app.get("/status")
def status():
    return {"results": [_result]}


@app.post("/run")
async def rerun():
    if _lock.locked():  # a (boot) capture is in flight — don't hang the request
        return {"ok": False, "result": {"interface": "camera", "status": "pending",
                                        "detail": "capture already in progress…", "data": {}}}
    await _do_test()
    return {"ok": _result["status"] == "pass", "result": _result}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
