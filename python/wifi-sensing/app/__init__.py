"""FastAPI app: wires the CSI pipeline, REST/WS routes, and the static SPA."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.config import Config
from app.lib.csi.ingest import UDPCSISource
from app.lib.csi.pipeline import Pipeline
from app.routes import sensing, system

logging.basicConfig(level=logging.INFO)

_config = Config.from_env()
_pipeline = Pipeline(_config)
_source = UDPCSISource(_config.udp_port)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pipeline = _pipeline
    ingest_task = asyncio.create_task(_pipeline.run(_source))
    analyze_task = asyncio.create_task(_pipeline.analyze_loop())
    try:
        yield
    finally:
        ingest_task.cancel()
        analyze_task.cancel()
        await _source.close()


api = FastAPI(lifespan=lifespan)

api.include_router(sensing.router, prefix="/api")
api.include_router(system.router, prefix="/api")
api.include_router(sensing.ws_router)

_static_dir = Path(__file__).resolve().parent.parent / "static"


@api.get("/{full_path:path}")
async def serve_spa(full_path: str):
    """Serve static files, fall back to index.html for SPA routing."""
    file_path = _static_dir / full_path
    if file_path.is_file():
        return FileResponse(file_path)
    return FileResponse(_static_dir / "index.html")
