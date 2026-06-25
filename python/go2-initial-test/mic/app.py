"""microphone test — subscribe to the Go2's mic over DDS and confirm audio frames.

The Go2 head mic publishes G.711 µ-law @ 8 kHz on `rt/audiosender`
(unitree_go AudioData) — it is NOT a local ALSA device, so the old sounddevice
capture could never work on the dog (it timed out with "no local input"). This
subscribes to the DDS stream like the other sensor tiles. Adapted from
/demos/go2-Watchtower/go2_audio_bridge.py and /demos/go2-camera/audio.py.
Pass = audio frames arriving; detail reports RMS so a live mic is
distinguishable from a near-silent room.

NOTE: do NOT add `from __future__ import annotations` — cyclonedds's IdlStruct
resolves type hints by name at class-definition time and PEP-563 breaks it.
"""
import audioop
import os
import socket
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import uvicorn
from fastapi import FastAPI

from cyclonedds.core import Policy, Qos
from cyclonedds.domain import DomainParticipant
from cyclonedds.idl import IdlStruct
from cyclonedds.idl.types import sequence, uint8, uint64
from cyclonedds.sub import DataReader, Subscriber
from cyclonedds.topic import Topic

PORT = int(os.environ.get("PORT", "3614"))
MIC_TOPIC = os.environ.get("MIC_TOPIC", "rt/audiosender")
CODEC = os.environ.get("MIC_CODEC", "ulaw").lower()  # G.711 µ-law @ 8 kHz (the dog's mic)
DDS_DOMAIN = int(os.environ.get("DDS_DOMAIN", "0"))
FRESH_S = float(os.environ.get("MIC_FRESH_S", "3.0"))
GO2_IP = os.environ.get("GO2_IP", "192.168.123.161")


def _resolve_dds_address(robot_ip):
    """Local IP this host uses to reach the Go2 — the address CycloneDDS must bind
    to (the Orin is multi-homed). GO2_DDS_ADDRESS overrides; otherwise ask the
    kernel which source IP routes to the robot (no packets sent, never blocks).
    Returns "" off-robot (no route)."""
    override = os.environ.get("GO2_DDS_ADDRESS", "").strip()
    if override:
        return override
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((robot_ip, 1))  # no traffic; the kernel just picks the route
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return ""


DDS_ADDR = _resolve_dds_address(GO2_IP)
# Bind CycloneDDS to that IP (the Orin is multi-homed). Off-robot we leave
# CYCLONEDDS_URI unset and DDS falls back to scanning all interfaces.
if DDS_ADDR:
    os.environ["CYCLONEDDS_URI"] = (
        "<CycloneDDS><Domain><General><Interfaces>"
        f'<NetworkInterface address="{DDS_ADDR}"/>'
        "</Interfaces></General></Domain></CycloneDDS>"
    )


@dataclass
class _AudioData(IdlStruct, typename="unitree_go::msg::dds_::AudioData_"):
    time_frame: uint64 = 0
    data: sequence[uint8] = field(default_factory=list)


app = FastAPI(title="go2-test-mic")
_last = {"rms": 0.0, "peak": 0.0, "samples": 0, "ts": 0.0}
_err: str | None = None


def _decode(payload: bytes) -> np.ndarray:
    """G.711 → float32 PCM in [-1, 1]."""
    pcm16 = audioop.alaw2lin(payload, 2) if CODEC == "alaw" else audioop.ulaw2lin(payload, 2)
    return np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0


def _run():
    global _err, _last
    while True:  # outer loop: (re)build the participant on any setup/read failure
        try:
            dp = DomainParticipant(DDS_DOMAIN)
            # Media streams are BEST_EFFORT — match the publisher or get nothing.
            qos = Qos(Policy.Reliability.BestEffort, Policy.History.KeepLast(10))
            topic = Topic(dp, MIC_TOPIC, _AudioData, qos=qos)
            reader = DataReader(Subscriber(dp), topic, qos=qos)
            _err = None
        except Exception as e:  # noqa: BLE001
            _err = (f"can't start DDS (auto-detected bind {DDS_ADDR or 'none — no route to the robot'}) — "
                    f"is this device on the Go2 LAN (192.168.123.x) and the dog powered? "
                    f"Set GO2_DDS_ADDRESS to override. [{type(e).__name__}]")
            time.sleep(1.0)
            continue
        while True:
            try:
                for msg in reader.take_iter(timeout=1_000_000_000):  # 1 s
                    if not msg.data:
                        continue
                    samples = _decode(bytes(msg.data))
                    if samples.size == 0:
                        continue
                    rms = float(np.sqrt(np.mean(samples ** 2)))
                    peak = float(np.max(np.abs(samples)))
                    _last = {"rms": round(rms, 5), "peak": round(peak, 4),
                             "samples": int(samples.size), "ts": time.time()}
            except Exception as e:  # noqa: BLE001
                _err = f"reader error: {e}"
                time.sleep(1.0)
                break  # rebuild the participant


@app.on_event("startup")
def _startup():
    threading.Thread(target=_run, name="mic-sub", daemon=True).start()


def _result():
    s = _last
    fresh = s["ts"] and (time.time() - s["ts"]) < FRESH_S
    if fresh:
        detail = f"mic streaming on {MIC_TOPIC} · RMS={s['rms']} peak={s['peak']} ({CODEC} 8 kHz)"
        if s["rms"] < 1e-4:
            detail += " (very quiet — mic live but near-silent; speak/clap)"
        return {"interface": "microphone", "status": "pass", "detail": detail, "data": s}
    # `na` (not fail): off-robot or the dog's audio service idle is expected and
    # shouldn't drag the board red — pass only when frames actually arrive.
    detail = _err or (f"no audio frames on {MIC_TOPIC} (auto-detected DDS bind "
                      f"{DDS_ADDR or 'none — no route to the robot'}) — is the dog powered and this "
                      "device on the robot LAN (192.168.123.x)?")
    return {"interface": "microphone", "status": "na", "detail": detail, "data": {}}


@app.get("/status")
def status():
    return {"results": [_result()]}


@app.post("/run")
def rerun():
    r = _result()
    return {"ok": r["status"] == "pass", "results": [r]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
