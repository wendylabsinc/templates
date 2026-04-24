"""HTTP + WebSocket entrypoint for the voice-ai-pipecat template.

Serves the built React visualizer from `./static/` and exposes a WebSocket at
`/bot-audio` for bidirectional PCM audio between the browser and the Pipecat
pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.runner import PipelineRunner
from pipecat.serializers.protobuf import ProtobufFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from pipeline import build_pipeline_task


PORT = int(os.environ.get("PORT", "3005"))
STATIC_DIR = Path(os.environ.get("STATIC_DIR", Path(__file__).parent / "static"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voice-ai-pipecat")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Eager model downloads on boot keep the first request snappy once the
    # persistent `/models` volume is warm.
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


@app.websocket("/bot-audio")
async def bot_audio(websocket: WebSocket) -> None:
    await websocket.accept()
    logger.info("Client connected")

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=16000,
            add_wav_header=False,
            serializer=ProtobufFrameSerializer(),
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    task = build_pipeline_task(transport)
    runner = PipelineRunner(handle_sigint=False)

    try:
        await runner.run(task)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Pipeline crashed")
        raise
    finally:
        logger.info("Client disconnected")


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
