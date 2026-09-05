import asyncio
import json

from fastapi import APIRouter, WebSocket

from app.lib.devices import list_cameras
from app.lib.gst_sink import GstCaptureSink

router = APIRouter()


class MJPEGCamera(GstCaptureSink):
    def _build_pipelines(self) -> list[str]:
        appsink = "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
        src = f"v4l2src device={self._current_device}" if self._current_device else "v4l2src"
        return [
            f"{src} ! image/jpeg ! {appsink}",
            f"{src} ! image/jpeg,width=640,height=480 ! {appsink}",
            f"{src} ! videoconvert ! jpegenc quality=70 ! {appsink}",
        ]


_camera = MJPEGCamera(max_queue=2)


@router.get("/cameras")
def get_cameras():
    return list_cameras()


@router.websocket("/camera/stream")
async def camera_stream(ws: WebSocket):
    await ws.accept()
    try:
        q = await _camera.add_client(ws)
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
                if "switch_camera" in msg:
                    _camera.switch_device(msg["switch_camera"])
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
        _camera.remove_client(ws)
