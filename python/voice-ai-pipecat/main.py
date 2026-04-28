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
`AUDIO_OUTPUT_DEVICE` env vars (PyAudio device index, name substring, or
`default`). The startup log prints the PyAudio enumeration so you can
find the right index. The `/api/audio-devices` endpoint exposes the same
list to the frontend so users can pick a device live.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
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
HOTPLUG_POLL_SECS = float(os.environ.get("HOTPLUG_POLL_SECS", "3.0"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voice-ai-pipecat")


def _enumerate_devices() -> list[dict[str, Any]]:
    """Return PyAudio's current device enumeration as plain dicts.

    Each PyAudio() instance probes ALSA fresh, so calling this on a poll
    interval is what lets us detect USB hot-plug events: an unplugged
    PowerConf disappears from the list, a freshly plugged one reappears
    (often at a new index, but the `name` is stable).
    """
    try:
        import pyaudio
    except Exception as exc:
        logger.warning("PyAudio unavailable for device enumeration: %s", exc)
        return []
    pa = pyaudio.PyAudio()
    try:
        devices = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            name = str(info.get("name", ""))
            # Skip kernel/virtual devices that aren't useful as user-facing
            # mics — Jetson "Orin Nano APE" enumerates ~20 virtual loopback
            # PCMs that just clutter the selector. Same for HDMI outputs.
            if "Orin Nano APE" in name or "Orin Nano HDA" in name:
                continue
            devices.append(
                {
                    "id": i,
                    "name": name,
                    "input_channels": int(info.get("maxInputChannels", 0)),
                    "output_channels": int(info.get("maxOutputChannels", 0)),
                    "default_sample_rate": int(info.get("defaultSampleRate", 0)),
                }
            )
        return devices
    finally:
        pa.terminate()


def _resolve_device(value: Optional[str], devices: list[dict[str, Any]]) -> tuple[Optional[int], Optional[str]]:
    """Resolve a device spec against a freshly-enumerated list.

    Returns `(index, name)`. Index is None when we want PortAudio's own
    default; name is the device label we matched (used by the hot-plug
    watchdog to re-find the device after a plug event shifts indexes).

    Accepts:
      - '' or None     → PortAudio default. Often wrong on ALSA — picks
                         card 0, which on Jetson is HDMI with no input.
      - 'default'      → device whose name is literally 'default'. With
                         our /etc/asound.conf routing ALSA default →
                         plug → hw:2,0, that's the entry that gets us
                         in-kernel rate conversion to the USB mic.
      - integer string → that exact index (no name match needed).
      - any other str  → first device whose name contains the substring,
                         case-insensitive (e.g. 'powerconf').
    """
    if not value:
        return None, None
    lookup = value.strip()
    try:
        idx = int(lookup)
    except ValueError:
        idx = None
    if idx is not None:
        for d in devices:
            if d["id"] == idx:
                return idx, d["name"]
        return None, None  # index not in current enum (probably unplugged)
    wanted = lookup.lower()
    for d in devices:
        name = d["name"].lower()
        if wanted == "default" and name == "default":
            return d["id"], d["name"]
        if wanted != "default" and wanted in name:
            return d["id"], d["name"]
    return None, None


def _find_device_index_by_name(name: str, devices: list[dict[str, Any]]) -> Optional[int]:
    for d in devices:
        if d["name"] == name:
            return d["id"]
    return None


def _log_audio_devices(devices: list[dict[str, Any]]) -> None:
    logger.info("PyAudio devices:")
    for d in devices:
        logger.info(
            "  [%d] %s  in=%d out=%d  default_sr=%d",
            d["id"],
            d["name"],
            d["input_channels"],
            d["output_channels"],
            d["default_sample_rate"],
        )


class SessionManager:
    """Keeps exactly one Pipecat pipeline running at a time.

    Default mode `local` uses `LocalAudioTransport` (host USB mic+speaker).
    When a browser connects to /bot-audio the local pipeline is torn down
    and a browser pipeline takes over.

    Tracks selected input/output devices by *name* in addition to index so
    the hot-plug watchdog can recover after an unplug/replug shifts ALSA
    enumeration order.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._mode: str = "idle"
        # Resolved at start_local time against the live enumeration. Index
        # may shift across hot-plug events; name is the stable handle.
        self._input_index: Optional[int] = None
        self._input_name: Optional[str] = None
        self._output_index: Optional[int] = None
        self._output_name: Optional[str] = None
        # Configured fallback (env vars). Used when no override is passed.
        self._configured_input = os.environ.get("AUDIO_INPUT_DEVICE")
        self._configured_output = os.environ.get("AUDIO_OUTPUT_DEVICE")
        self._last_error: Optional[str] = None
        self._device_missing: bool = False

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def device_missing(self) -> bool:
        return self._device_missing

    @property
    def input_name(self) -> Optional[str]:
        return self._input_name

    @property
    def output_name(self) -> Optional[str]:
        return self._output_name

    def is_owned_by(self, task: asyncio.Task) -> bool:
        return self._task is task

    async def start_local(
        self,
        *,
        input_device: Optional[str] = None,
        output_device: Optional[str] = None,
    ) -> Optional[asyncio.Task]:
        """Start (or restart) the local audio pipeline.

        `input_device`/`output_device` accept the same forms as the
        `AUDIO_*_DEVICE` env vars (index, name substring, 'default').
        Passing None falls back to whatever was configured at startup.

        Returns the running pipeline task on success, or None if the
        configured input device is currently missing (e.g. USB unplugged).
        In the missing case `device_missing` is set to True and the
        hot-plug watchdog will retry once the device reappears.
        """
        in_spec = input_device if input_device is not None else self._configured_input
        out_spec = output_device if output_device is not None else self._configured_output

        devices = _enumerate_devices()
        in_idx, in_name = _resolve_device(in_spec, devices)
        out_idx, out_name = _resolve_device(out_spec, devices)

        # Persist what the user (or env) asked for; the resolved index/name
        # may shift across hot-plug events so we re-resolve each time.
        if input_device is not None:
            self._configured_input = input_device
        if output_device is not None:
            self._configured_output = output_device

        # If a specific input was requested but isn't present, don't try
        # to start PortAudio — it would silently fail or kill the app.
        # Mark device-missing so /api/status surfaces it to the frontend
        # Alert; the watchdog will start the pipeline once the device
        # comes back.
        if in_spec and in_idx is None:
            async with self._lock:
                await self._cancel_current_locked()
                self._mode = "idle"
                self._input_index = None
                self._input_name = None
                self._output_index = out_idx
                self._output_name = out_name
                self._device_missing = True
                self._last_error = (
                    f"Audio device {in_spec!r} not found in current "
                    "PyAudio enumeration. Plug it in (or pick another "
                    "device) and the local pipeline will resume."
                )
                logger.warning(self._last_error)
            return None

        self._input_index = in_idx
        self._input_name = in_name
        self._output_index = out_idx
        self._output_name = out_name

        in_rate = int(os.environ.get("LOCAL_AUDIO_IN_SAMPLE_RATE", "16000"))
        out_rate = int(os.environ.get("LOCAL_AUDIO_OUT_SAMPLE_RATE", "48000"))
        transport = LocalAudioTransport(
            LocalAudioTransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=in_rate,
                audio_out_sample_rate=out_rate,
                vad_analyzer=SileroVADAnalyzer(),
                input_device_index=in_idx,
                output_device_index=out_idx,
            )
        )
        self._device_missing = False
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

    async def mark_device_lost(self, message: str) -> None:
        """Called by the watchdog when the configured input device vanishes."""
        async with self._lock:
            await self._cancel_current_locked()
            self._mode = "idle"
            self._last_error = message
            self._device_missing = True
            logger.warning("SessionManager: device lost — %s", message)

    async def _switch_to(self, transport: BaseTransport, *, mode: str) -> asyncio.Task:
        async with self._lock:
            await self._cancel_current_locked()
            self._mode = mode
            self._last_error = None
            self._task = asyncio.create_task(self._run_pipeline(transport, mode))
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

    async def _run_pipeline(self, transport: BaseTransport, mode: str) -> None:
        task = build_pipeline_task(transport)
        runner = PipelineRunner(handle_sigint=False)
        try:
            await runner.run(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("SessionManager: %s pipeline crashed", mode)
            self._last_error = f"{mode}: {exc}"


session = SessionManager()


async def _hotplug_watchdog() -> None:
    """Detect USB hot-plug events affecting the local audio pipeline.

    Re-resolves the configured input spec against a fresh PyAudio enum
    every HOTPLUG_POLL_SECS. Two transitions matter:
      - Spec was resolvable, now isn't → tear down local pipeline, mark
        device-missing so the frontend Alert can surface it.
      - Spec wasn't resolvable, now is → restart local pipeline.

    Browser mode is unaffected: the pipeline reads audio from the WS, not
    from PortAudio, so it survives a USB unplug.
    """
    while True:
        try:
            await asyncio.sleep(HOTPLUG_POLL_SECS)
            spec = session._configured_input  # noqa: SLF001 — internal access
            if not spec:
                # No specific device was requested (PortAudio default).
                # Nothing to watch for.
                continue
            devices = _enumerate_devices()
            in_idx, _in_name = _resolve_device(spec, devices)
            present = in_idx is not None

            if session.mode == "local" and not present:
                await session.mark_device_lost(
                    f"Audio device {spec!r} disappeared (USB unplug?)"
                )
            elif session.device_missing and present:
                logger.info(
                    "Hotplug: %r reappeared, restarting local pipeline", spec
                )
                try:
                    await session.start_local()
                except Exception:
                    logger.exception("Hotplug: failed to restart local pipeline")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Hotplug watchdog error")


@asynccontextmanager
async def lifespan(_: FastAPI):
    devices = _enumerate_devices()
    _log_audio_devices(devices)
    await session.start_local()
    watchdog = asyncio.create_task(_hotplug_watchdog())
    try:
        yield
    finally:
        watchdog.cancel()
        try:
            await watchdog
        except asyncio.CancelledError:
            pass
        await session.stop()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


# --- Audio device API for the frontend "WendyOS devices" combobox -----------


@app.get("/api/audio-devices")
async def api_audio_devices() -> dict[str, Any]:
    """Live enumeration of host audio devices for the device-side selector."""
    devices = _enumerate_devices()
    return {
        "devices": devices,
        "selected": {
            "input_name": session.input_name,
            "output_name": session.output_name,
        },
    }


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        "mode": session.mode,
        "input_name": session.input_name,
        "output_name": session.output_name,
        "device_missing": session.device_missing,
        "error": session.last_error,
    }


class LocalAudioSelectBody(BaseModel):
    input_id: Optional[str] = None
    output_id: Optional[str] = None


@app.post("/api/local-audio/select")
async def api_local_audio_select(body: LocalAudioSelectBody) -> dict[str, Any]:
    """Restart the local pipeline using new input/output devices.

    Body fields accept anything `_resolve_device` understands (integer
    index, name substring, 'default'). Either field may be omitted to
    keep the current selection.
    """
    if body.input_id is None and body.output_id is None:
        raise HTTPException(status_code=400, detail="input_id or output_id required")
    try:
        await session.start_local(
            input_device=body.input_id,
            output_device=body.output_id,
        )
    except Exception as exc:
        logger.exception("Failed to switch local devices")
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "mode": session.mode,
        "input_name": session.input_name,
        "output_name": session.output_name,
    }


# --- Browser audio WebSocket ------------------------------------------------


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
        if pipeline_task is not None and session.is_owned_by(pipeline_task):
            try:
                await session.start_local()
            except Exception:
                logger.exception("Failed to resume local pipeline")


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
