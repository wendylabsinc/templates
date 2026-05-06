"""Sanity test: synthesize a 1-second beep WAV, upload to the dog,
and play it via the audiohub's documented file API.

If this beeps the dog → megaphone playback works; the missing piece
for live streaming is just the right topic / track.

If this is silent → audiohub is gated off entirely on this firmware
or needs additional setup we haven't discovered yet.

Run inside the go2-camera container:
    python /tmp/test_megaphone.py
"""

import asyncio
import logging
import math
import struct
import tempfile
import wave

from unitree_webrtc_connect import (
    UnitreeWebRTCConnection,
    WebRTCConnectionMethod,
)
from unitree_webrtc_connect.webrtc_audiohub import WebRTCAudioHub


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("megaphone-test")


def make_beep_wav(path: str, freq_hz: float = 880.0,
                  duration_s: float = 1.5, rate: int = 16000) -> None:
    """Write a clean sine-wave WAV — 16-bit PCM mono, 16 kHz."""
    n = int(duration_s * rate)
    amp = 0.4 * 32767
    samples = [
        int(amp * math.sin(2 * math.pi * freq_hz * i / rate))
        for i in range(n)
    ]
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", s) for s in samples))


async def main() -> None:
    log.info("Connecting…")
    conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip="192.168.123.161")
    await conn.connect()
    log.info("Connected; setting up audiohub")
    hub = WebRTCAudioHub(conn)

    # Path 1: enter megaphone, upload, hope it auto-plays.
    await hub.enter_megaphone()
    log.info("Entered megaphone mode")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
    make_beep_wav(wav_path)
    log.info("Beep WAV written to %s", wav_path)

    log.info("Calling upload_megaphone(%s)…", wav_path)
    result = hub.upload_megaphone(wav_path)
    if asyncio.iscoroutine(result):
        result = await result
    log.info("upload_megaphone returned: %r", result)

    # Some firmwares return a uuid we can use with play_by_uuid.
    uuid = result if isinstance(result, str) else None
    if uuid:
        log.info("Calling play_by_uuid(%s)…", uuid)
        r = hub.play_by_uuid(uuid)
        if asyncio.iscoroutine(r):
            r = await r
        log.info("play_by_uuid returned: %r", r)

    log.info("Sleeping 5 s so any beep finishes before we exit…")
    await asyncio.sleep(5)

    log.info("Exiting megaphone")
    r = hub.exit_megaphone()
    if asyncio.iscoroutine(r):
        await r
    log.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
