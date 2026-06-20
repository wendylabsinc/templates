"""speaker test — play a tone on the Go2 via the WebRTC audiohub megaphone.

MANUAL: the dashboard "Run test" button POSTs /run, which connects over WebRTC,
enters megaphone mode, uploads a short beep WAV, plays it on the dog, then exits;
a human confirms they heard it (we can't verify audio output programmatically).

The old DDS `rt/audioreceiver` publish was unreliable — the demos found it
produces "pure noise" and is NOT the live-playback channel. The audiohub
megaphone file API is what actually plays. Adapted from
/demos/go2-camera/scripts/test_megaphone.py.

NOTE: the Go2 allows only ONE WebRTC client, so this contends with the camera
test (and any other WebRTC app like go2-rc's stream) — only one can hold the slot
at a time.
"""
import asyncio
import math
import os
import struct
import tempfile
import wave

import uvicorn
from fastapi import FastAPI
from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.webrtc_audiohub import WebRTCAudioHub

PORT = int(os.environ.get("PORT", "3615"))
GO2_IP = os.environ.get("GO2_IP", "192.168.123.161")
TONE_HZ = float(os.environ.get("TONE_HZ", "880"))
TONE_SECONDS = float(os.environ.get("TONE_SECONDS", "1.5"))

app = FastAPI(title="go2-test-speaker")
_lock = asyncio.Lock()
_result = {"interface": "speaker", "status": "manual",
           "detail": "press “Run test” to play a beep on the dog (WebRTC megaphone), then confirm you heard it",
           "data": {}}


def _make_beep_wav(path: str) -> None:
    """Clean sine WAV — 16-bit PCM mono, 16 kHz (what the megaphone expects)."""
    rate = 16000
    n = int(TONE_SECONDS * rate)
    amp = 0.4 * 32767
    samples = (int(amp * math.sin(2 * math.pi * TONE_HZ * i / rate)) for i in range(n))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", s) for s in samples))


async def _maybe_await(v):
    return await v if asyncio.iscoroutine(v) else v


async def _play():
    global _result
    conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=GO2_IP)
    try:
        await conn.connect()
        hub = WebRTCAudioHub(conn)
        await hub.enter_megaphone()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = f.name
        _make_beep_wav(wav_path)
        result = await _maybe_await(hub.upload_megaphone(wav_path))
        # Some firmwares auto-play on upload; others return a uuid to play explicitly.
        uuid = result if isinstance(result, str) else None
        if uuid:
            await _maybe_await(hub.play_by_uuid(uuid))
        await asyncio.sleep(TONE_SECONDS + 1)  # let the beep finish before exiting
        await _maybe_await(hub.exit_megaphone())
        _result = {"interface": "speaker", "status": "manual",
                   "detail": f"played {TONE_HZ:.0f} Hz beep ({TONE_SECONDS:.1f}s) via WebRTC megaphone. "
                             "Heard it? (if silent, the firmware may gate the audiohub)",
                   "data": {"uuid": uuid}}
    except Exception as e:  # noqa: BLE001
        _result = {"interface": "speaker", "status": "fail",
                   "detail": f"megaphone playback failed: {e} — is the Go2 reachable at {GO2_IP} and the "
                             "single WebRTC slot free (camera/go2-rc not holding it)?",
                   "data": {}}
    finally:
        try:
            await conn.close()  # release the single WebRTC slot
        except Exception:  # noqa: BLE001
            pass


@app.get("/status")
def status():
    return {"results": [_result]}


@app.post("/run")
async def rerun():
    # One playback at a time — concurrent WebRTC connects fight over the single slot.
    if _lock.locked():
        return {"ok": False, "result": {"interface": "speaker", "status": "manual",
                                        "detail": "already playing — try again in a moment", "data": {}}}
    async with _lock:
        await _play()
    return {"ok": _result["status"] != "fail", "result": _result}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
