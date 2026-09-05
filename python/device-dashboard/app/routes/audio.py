import asyncio
import json

from fastapi import APIRouter, WebSocket

from app.lib.devices import list_alsa_devices
from app.lib.gst_sink import GstCaptureSink

router = APIRouter()


class AudioCapture(GstCaptureSink):
    def _build_pipelines(self) -> list[str]:
        appsink = "appsink name=sink emit-signals=true max-buffers=4 drop=true sync=false"
        pcm = "audio/x-raw,format=S16LE,channels=1,rate=16000"

        pipelines = []
        if self._current_device:
            pipelines.append(
                f'alsasrc device="{self._current_device}" ! audioconvert ! audioresample ! {pcm} ! {appsink}'
            )
        else:
            for mic in list_alsa_devices("arecord -l"):
                pipelines.append(
                    f'alsasrc device="{mic["id"]}" ! audioconvert ! audioresample ! {pcm} ! {appsink}'
                )
            pipelines.append(
                f"alsasrc ! audioconvert ! audioresample ! {pcm} ! {appsink}"
            )
        return pipelines


_audio = AudioCapture(max_queue=4)


@router.get("/microphones")
def get_microphones():
    return list_alsa_devices("arecord -l")


@router.get("/speakers")
def get_speakers():
    return list_alsa_devices("aplay -l")


@router.websocket("/audio/stream")
async def audio_stream(ws: WebSocket):
    await ws.accept()
    try:
        q = await _audio.add_client(ws)
    except Exception:
        await ws.close(1011)
        return

    async def send():
        try:
            while True:
                await ws.send_bytes(await q.get())
        except Exception:
            pass

    async def recv():
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                if "switch_microphone" in msg:
                    _audio.switch_device(msg["switch_microphone"])
        except Exception:
            pass

    try:
        done, pending = await asyncio.wait(
            [asyncio.create_task(send()), asyncio.create_task(recv())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        _audio.remove_client(ws)
