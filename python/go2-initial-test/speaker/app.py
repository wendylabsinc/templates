"""speaker test — publish a test tone to rt/audioreceiver (the dog's speaker).

MANUAL: the dashboard "Run test" button POSTs /run, which plays a ~1 s tone;
a human confirms they heard it (we can't verify audio output programmatically).
AudioData IDL + publisher adapted from /demos/go2-camera/audio.py.

Do NOT add `from __future__ import annotations` (IdlStruct name-resolves hints).
"""
import audioop
import math
import os
import struct
import threading
import time
from dataclasses import dataclass, field

import uvicorn
from fastapi import FastAPI

from cyclonedds.domain import DomainParticipant
from cyclonedds.idl import IdlStruct
from cyclonedds.idl.types import sequence, uint8, uint64
from cyclonedds.pub import DataWriter, Publisher
from cyclonedds.topic import Topic

PORT = int(os.environ.get("PORT", "3615"))
TOPIC = os.environ.get("AUDIO_OUT_TOPIC", "rt/audioreceiver")
CODEC = os.environ.get("AUDIO_OUT_CODEC", "pcm16").lower()
DDS_DOMAIN = int(os.environ.get("DDS_DOMAIN", "0"))
RATE = 8000
FREQ = float(os.environ.get("TONE_HZ", "440"))
SECONDS = float(os.environ.get("TONE_SECONDS", "1.0"))
FRAME_SAMPLES = 160  # 20 ms @ 8 kHz


@dataclass
class _AudioData(IdlStruct, typename="unitree_go::msg::dds_::AudioData_"):
    time_frame: uint64 = 0
    data: sequence[uint8] = field(default_factory=list)


app = FastAPI(title="go2-test-speaker")
_writer = None
_play_lock = threading.Lock()
_result = {"interface": "speaker", "status": "manual",
           "detail": "press “Run test” to play a tone on the dog, then confirm you heard it",
           "data": {}}


def _ensure_writer():
    global _writer
    if _writer is None:
        dp = DomainParticipant(DDS_DOMAIN)
        _writer = DataWriter(Publisher(dp), Topic(dp, TOPIC, _AudioData))
    return _writer


def _tone_pcm16() -> bytes:
    n = int(RATE * SECONDS)
    return b"".join(struct.pack("<h", int(0.6 * 32767 * math.sin(2 * math.pi * FREQ * i / RATE)))
                    for i in range(n))


def _encode(pcm: bytes) -> bytes:
    if CODEC == "ulaw":
        return audioop.lin2ulaw(pcm, 2)
    if CODEC == "alaw":
        return audioop.lin2alaw(pcm, 2)
    return pcm  # pcm16


def _play():
    global _result
    try:
        w = _ensure_writer()
        payload = _encode(_tone_pcm16())
        step = FRAME_SAMPLES * (1 if CODEC in ("ulaw", "alaw") else 2)
        frames = 0
        for off in range(0, len(payload), step):
            frames += 1
            w.write(_AudioData(time_frame=frames, data=list(payload[off:off + step])))
            time.sleep(FRAME_SAMPLES / RATE)  # real-time pacing
        _result = {"interface": "speaker", "status": "manual",
                   "detail": f"played {FREQ:.0f} Hz tone ({SECONDS:.0f}s, {CODEC}) → {TOPIC}. "
                             "Heard it? If silent, try AUDIO_OUT_CODEC=ulaw.",
                   "data": {"frames": frames}}
    except Exception as e:  # noqa: BLE001
        _result = {"interface": "speaker", "status": "fail",
                   "detail": f"publish failed: {e}", "data": {}}


@app.get("/status")
def status():
    return {"results": [_result]}


@app.post("/run")
def rerun():
    # One playback at a time — concurrent writes interleave frames (garbled tone).
    if not _play_lock.acquire(blocking=False):
        return {"ok": False, "result": {"interface": "speaker", "status": "manual",
                                        "detail": "already playing — try again in a moment", "data": {}}}
    try:
        _play()
    finally:
        _play_lock.release()
    return {"ok": _result["status"] != "fail", "result": _result}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
