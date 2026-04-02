import logging
import threading
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")

from gi.repository import Gst, GLib
from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.routes import data, camera, audio, gpu, system

logging.basicConfig(level=logging.INFO)

Gst.init(None)
_glib_loop = GLib.MainLoop()
threading.Thread(target=_glib_loop.run, daemon=True).start()

api = FastAPI()

api.include_router(data.router, prefix="/api")
api.include_router(camera.router, prefix="/api")
api.include_router(audio.router, prefix="/api")
api.include_router(gpu.router, prefix="/api")
api.include_router(system.router, prefix="/api")

_static_dir = Path(__file__).resolve().parent.parent / "static"


@api.get("/{full_path:path}")
async def serve_spa(full_path: str):
    """Serve static files, fall back to index.html for SPA routing."""
    file_path = _static_dir / full_path
    if file_path.is_file():
        return FileResponse(file_path)
    return FileResponse(_static_dir / "index.html")
