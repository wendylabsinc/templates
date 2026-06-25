"""go2-foxglove camera — Go2 front camera (WebRTC) → JPEG → forward to the bridge.

Keeps the heavy WebRTC/opencv stack in its own container. Decoded frames are
POSTed to the bridge's localhost ingest endpoint, which republishes them on the
`/go2/camera` Foxglove channel — so the camera shows up on the SAME single
Foxglove connection as the LiDAR/pose/state, but a WebRTC failure here can't take
the bridge (3D view) down with it.

The Go2 allows only ONE WebRTC client — if the Unitree phone app is connected,
this can't connect until it disconnects.

UNVERIFIED on a live Go2 EDU+. Verify the `unitree_webrtc_connect` API + that the
front camera is reachable at GO2_IP.
"""
import asyncio
import logging
import os
import time

import cv2
import httpx
from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("go2-foxglove-camera")

GO2_IP = os.environ.get("GO2_IP", "192.168.123.161")
INGEST_URL = os.environ.get("BRIDGE_INGEST_URL", "http://127.0.0.1:8766/frame")
MAX_FPS = float(os.environ.get("MAX_FPS", "12"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "80"))

_min_dt = 1.0 / max(1.0, MAX_FPS)


async def _pump(track, client: httpx.AsyncClient):
    last = 0.0
    while True:
        try:
            frame = await track.recv()
            now = time.time()
            if now - last < _min_dt:  # throttle the forward rate
                continue
            last = now
            img = frame.to_ndarray(format="bgr24")
            ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if not ok:
                continue
            await client.post(INGEST_URL, content=jpg.tobytes(),
                              headers={"Content-Type": "image/jpeg"}, timeout=2.0)
        except Exception:  # noqa: BLE001 — track drop / bridge briefly down: keep going
            await asyncio.sleep(0.2)


async def _run_once():
    conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=GO2_IP)
    async with httpx.AsyncClient() as client:
        try:
            await conn.connect()
            conn.video.switchVideoChannel(True)
            conn.video.add_track_callback(lambda t: asyncio.ensure_future(_pump(t, client)))
            log.info("WebRTC connected to %s; forwarding JPEGs to %s", GO2_IP, INGEST_URL)
            while True:
                await asyncio.sleep(3600)
        finally:
            try:
                await conn.close()  # release the Go2's single WebRTC slot
            except Exception:  # noqa: BLE001
                pass


async def main():
    while True:
        try:
            await _run_once()
        except Exception as e:  # noqa: BLE001
            log.warning("WebRTC session failed (%s); retrying in 5s — is the Go2 reachable at %s, "
                        "and is the phone app holding the WebRTC slot?", e, GO2_IP)
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
