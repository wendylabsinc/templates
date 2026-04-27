"""HTTP + WebSocket entrypoint for the voice-ai-pipecat template.

Serves the built React visualizer from `./static/` and runs a Pipecat
pipeline in one of two transports at a time:

- **local** (default): `LocalAudioTransport` reads from the host's USB mic
  and writes to its speaker via PortAudio. Lets you talk to the assistant
  with no browser involved.
- **browser**: when a client connects to `/bot-audio`, the local pipeline
  is torn down and a `FastAPIWebsocketTransport` takes over. On browser
  disconnect (or via the "Hand back to local mic" button) the local
  pipeline resumes.

Pick the local mic/speaker via the `AUDIO_INPUT_DEVICE` /
`AUDIO_OUTPUT_DEVICE` env vars (PyAudio device index, or `default`).
The startup log prints the PyAudio enumeration so you can find the
right index for your device.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.runner import PipelineRunner
from pipecat.serializers.protobuf import ProtobufFrameSerializer
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.local.audio import (
    LocalAudioTransport,
    LocalAudioTransportParams,
)
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from pipeline import build_pipeline_task


PORT = int(os.environ.get("PORT", "3005"))
STATIC_DIR = Path(os.environ.get("STATIC_DIR", Path(__file__).parent / "static"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voice-ai-pipecat")


def _parse_audio_device(value: Optional[str]) -> Optional[int]:
    """Resolve a device spec to a PyAudio device index.

    Accepts:
      - '' or None                → PortAudio's built-in default (often wrong:
                                    on ALSA it picks card 0 which on Jetson
                                    is HDMI with no input)
      - 'default'                 → the PyAudio device whose name is literally
                                    'default'. With our /etc/asound.conf
                                    routing ALSA default → plug → hw:2,0,
                                    that's the entry that gives us in-kernel
                                    rate conversion to the USB mic.
      - integer string (e.g. '24') → that exact index
      - any other string          → first PyAudio device whose name contains
                                    the substring (case-insensitive), e.g.
                                    'powerconf'
    """
    if not value:
        return None
    lookup = value.strip()
    try:
        return int(lookup)
    except ValueError:
        pass
    try:
        import pyaudio
    except Exception as exc:
        logger.warning("PyAudio unavailable for device lookup of %r: %s", lookup, exc)
        return None
    pa = pyaudio.PyAudio()
    try:
        wanted = lookup.lower()
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            name = str(info.get("name", "")).lower()
            if wanted == "default" and name == "default":
                return i
            if wanted != "default" and wanted in name:
                return i
    finally:
        pa.terminate()
    logger.warning("No PyAudio device matched %r; falling back to PortAudio default", value)
    return None


def _log_audio_devices() -> None:
    """Log PyAudio's device enumeration so operators can pick AUDIO_*_DEVICE indexes."""
    try:
        import pyaudio  # local import: only needed at boot, missing is non-fatal
    except Exception as exc:
        logger.warning("PyAudio unavailable for device listing: %s", exc)
        return
    pa = pyaudio.PyAudio()
    try:
        logger.info("PyAudio devices:")
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            logger.info(
                "  [%d] %s  in=%d out=%d  default_sr=%.0f",
                i,
                info.get("name"),
                info.get("maxInputChannels"),
                info.get("maxOutputChannels"),
                info.get("defaultSampleRate", 0),
            )
    finally:
        pa.terminate()


class SessionManager:
    """Keeps exactly one Pipecat pipeline running at a time.

    Default mode `local` uses `LocalAudioTransport` (host USB mic+speaker).
    When a browser connects to /bot-audio the local pipeline is torn down
    and a browser pipeline takes over. On browser disconnect the WS
    handler triggers a local restart, but only if it still owns the active
    task — `is_owned_by` lets a second-browser-bumps-first scenario work
    cleanly.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._mode: str = "idle"
        self._input_device = _parse_audio_device(os.environ.get("AUDIO_INPUT_DEVICE"))
        self._output_device = _parse_audio_device(os.environ.get("AUDIO_OUTPUT_DEVICE"))

    @property
    def mode(self) -> str:
        return self._mode

    def is_owned_by(self, task: asyncio.Task) -> bool:
        return self._task is task

    async def start_local(self) -> asyncio.Task:
        # Input and output rates may need to differ:
        #  - Silero VAD requires 16 kHz or 8 kHz exactly (other rates raise),
        #    so input must be 16 kHz.
        #  - Many USB devices (e.g. Anker PowerConf) refuse 16 kHz playback
        #    via direct ALSA hw:N,M (PortAudio returns paInvalidSampleRate=
        #    -9997). They typically only do their native rate (48 kHz).
        # USB mics generally support 16 kHz capture even when their speaker
        # only does 48 kHz, so split the rates by direction.
        in_rate = int(os.environ.get("LOCAL_AUDIO_IN_SAMPLE_RATE", "16000"))
        out_rate = int(os.environ.get("LOCAL_AUDIO_OUT_SAMPLE_RATE", "48000"))
        transport = LocalAudioTransport(
            LocalAudioTransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=in_rate,
                audio_out_sample_rate=out_rate,
                vad_analyzer=SileroVADAnalyzer(),
                input_device_index=self._input_device,
                output_device_index=self._output_device,
            )
        )
        return await self._switch_to(transport, mode="local")

    async def start_browser(self, websocket: WebSocket) -> asyncio.Task:
        transport = FastAPIWebsocketTransport(
            websocket=websocket,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=16000,
                audio_out_sample_rate=16000,
                add_wav_header=False,
                vad_analyzer=SileroVADAnalyzer(),
                serializer=ProtobufFrameSerializer(),
            ),
        )
        return await self._switch_to(transport, mode="browser")

    async def stop(self) -> None:
        async with self._lock:
            await self._cancel_current_locked()
            self._mode = "idle"

    async def _switch_to(self, transport: BaseTransport, *, mode: str) -> asyncio.Task:
        async with self._lock:
            await self._cancel_current_locked()
            self._mode = mode
            self._task = asyncio.create_task(self._run_pipeline(transport))
            logger.info("SessionManager: %s pipeline started", mode)
            return self._task

    async def _cancel_current_locked(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("SessionManager: pipeline shutdown error")
        self._task = None

    async def _run_pipeline(self, transport: BaseTransport) -> None:
        task = build_pipeline_task(transport)
        runner = PipelineRunner(handle_sigint=False)
        try:
            await runner.run(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("SessionManager: pipeline crashed")


session = SessionManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    _log_audio_devices()
    await session.start_local()
    try:
        yield
    finally:
        await session.stop()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


async def _wait_until_ws_closed(websocket: WebSocket) -> None:
    """Poll the FastAPI WebSocket state until the client disconnects.

    Pipecat 0.0.108's FastAPIWebsocketTransport doesn't propagate WS closure
    back into the pipeline, so we watch the socket state ourselves and
    cancel the pipeline task when it goes DISCONNECTED. Without this,
    pressing "Hand back to local mic" closes the WS but leaves the pipeline
    task hanging forever.
    """
    while (
        websocket.client_state == WebSocketState.CONNECTED
        and websocket.application_state == WebSocketState.CONNECTED
    ):
        await asyncio.sleep(0.5)


@app.websocket("/bot-audio")
async def bot_audio(websocket: WebSocket) -> None:
    await websocket.accept()
    logger.info("Browser connected")

    pipeline_task: Optional[asyncio.Task] = None
    try:
        pipeline_task = await session.start_browser(websocket)
        watcher = asyncio.create_task(_wait_until_ws_closed(websocket))
        try:
            await asyncio.wait(
                {pipeline_task, watcher},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            watcher.cancel()
            # If the WS closed first, the pipeline is still running — cancel
            # it so we can resume local mode below.
            if not pipeline_task.done():
                pipeline_task.cancel()
                try:
                    await pipeline_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("Pipeline error during shutdown")
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception:
        logger.exception("Browser pipeline error")
    finally:
        logger.info("Browser disconnected")
        # Resume local mic only if WE were the active session at end-of-life.
        # If a second browser bumped us, is_owned_by is False and we leave
        # the newer session alone.
        if pipeline_task is not None and session.is_owned_by(pipeline_task):
            try:
                await session.start_local()
            except Exception:
                logger.exception("Failed to resume local pipeline")


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
