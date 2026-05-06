"""go2-broadcaster — push the robot's MJPEG camera feed to Twitch.

Wraps an ffmpeg subprocess that:
  - reads MJPEG from a sibling (realsense or go2-camera by default)
  - re-encodes to H.264 at a Twitch-friendly bitrate
  - pushes RTMP to live.twitch.tv

Designed to run alongside go2-RC (and realsense) on the dog's host
network. Intentionally has no UI — control plane is /api/start,
/api/stop, /api/status. Auto-starts on container boot if AUTOSTART=true
(default), provided TWITCH_STREAM_KEY is set.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("go2-broadcaster")


CAMERA_UPSTREAM_URL = os.environ.get(
    "CAMERA_UPSTREAM_URL", "http://127.0.0.1:8000/stream/color"
).strip()
TWITCH_INGEST_URL = (
    os.environ.get("TWITCH_INGEST_URL", "rtmp://live.twitch.tv/app").strip().rstrip("/")
)
TWITCH_STREAM_KEY = os.environ.get("TWITCH_STREAM_KEY", "").strip()
PORT = int(os.environ.get("PORT", "3700"))
BITRATE_K = int(os.environ.get("BITRATE_K", "4500"))
FRAMERATE = int(os.environ.get("FRAMERATE", "30"))
GOP = int(os.environ.get("GOP", "60"))
AUTOSTART = os.environ.get("AUTOSTART", "true").strip().lower() not in ("0", "false", "no")
RESTART_BACKOFF_S = float(os.environ.get("RESTART_BACKOFF_S", "5.0"))


class _State:
    """In-memory broadcast state. Survives one container lifetime, not more."""

    def __init__(self) -> None:
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.want_running: bool = False
        self.last_error: Optional[str] = None
        self.started_at: Optional[float] = None
        self._supervisor_task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None


_state = _State()


def _ffmpeg_args() -> list[str]:
    """Build the ffmpeg command. Tuned for Twitch ingest at 720p30."""
    if not TWITCH_STREAM_KEY:
        raise RuntimeError("TWITCH_STREAM_KEY is unset")
    if not CAMERA_UPSTREAM_URL:
        raise RuntimeError("CAMERA_UPSTREAM_URL is unset")

    target = f"{TWITCH_INGEST_URL}/{TWITCH_STREAM_KEY}"
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-f", "mjpeg",
        "-i", CAMERA_UPSTREAM_URL,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-b:v", f"{BITRATE_K}k",
        "-maxrate", f"{BITRATE_K}k",
        "-bufsize", f"{BITRATE_K * 2}k",
        "-g", str(GOP),
        "-r", str(FRAMERATE),
        # No audio. Robot mic capture is a separate concern handled by
        # go2-camera; pulling it through here would add a second IPC hop
        # we don't need for the public broadcast.
        "-an",
        "-f", "flv",
        target,
    ]


async def _start_proc() -> None:
    """Spawn one ffmpeg. Caller drives the supervisor loop."""
    args = _ffmpeg_args()
    # Never log the stream key. The key is embedded inside the RTMP
    # URL (rtmp://.../app/<key>), so a string equality check on each
    # arg won't catch it — substring-replace every arg instead.
    redacted = " ".join(a.replace(TWITCH_STREAM_KEY, "***") for a in args[1:])
    logger.info("starting ffmpeg: %s", redacted)

    _state.proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _state.started_at = time.time()


async def _supervisor() -> None:
    """Keep ffmpeg alive while want_running. Backs off on rapid failure."""
    while _state.want_running:
        try:
            await _start_proc()
        except Exception as exc:
            _state.last_error = str(exc)
            logger.error("ffmpeg start failed: %s", exc)
            await asyncio.sleep(RESTART_BACKOFF_S)
            continue

        assert _state.proc is not None
        rc = await _state.proc.wait()
        if _state.proc.stderr is not None:
            try:
                tail = (await _state.proc.stderr.read()).decode(errors="replace")
                # ffmpeg can echo the full output URL — including the
                # stream key — in errors when the RTMP push fails. Strip
                # before logging or surfacing via /api/status.
                if TWITCH_STREAM_KEY:
                    tail = tail.replace(TWITCH_STREAM_KEY, "***")
                if tail.strip():
                    _state.last_error = tail.splitlines()[-1]
                    logger.warning("ffmpeg stderr tail: %s", tail[-2000:])
            except Exception:
                pass

        logger.info("ffmpeg exited rc=%s want_running=%s", rc, _state.want_running)
        if not _state.want_running:
            break
        # Back off so we don't hammer Twitch ingest if creds/URL are wrong.
        await asyncio.sleep(RESTART_BACKOFF_S)

    _state.proc = None
    _state.started_at = None


async def _start() -> None:
    if _state.want_running:
        return
    _state.want_running = True
    _state.last_error = None
    _state._supervisor_task = asyncio.create_task(_supervisor())


async def _stop() -> None:
    _state.want_running = False
    if _state.proc is not None and _state.proc.returncode is None:
        try:
            _state.proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(_state.proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                _state.proc.kill()
                await _state.proc.wait()
        except ProcessLookupError:
            pass
    if _state._supervisor_task is not None:
        try:
            await _state._supervisor_task
        except Exception:
            pass
        _state._supervisor_task = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info(
        "go2-broadcaster up. camera=%s ingest=%s key=%s autostart=%s port=%d",
        CAMERA_UPSTREAM_URL,
        TWITCH_INGEST_URL,
        "set" if TWITCH_STREAM_KEY else "MISSING",
        AUTOSTART,
        PORT,
    )
    if AUTOSTART and TWITCH_STREAM_KEY and CAMERA_UPSTREAM_URL:
        await _start()
    try:
        yield
    finally:
        await _stop()


app = FastAPI(title="go2-broadcaster", lifespan=lifespan)


class StatusResponse(BaseModel):
    running: bool
    want_running: bool
    started_at: Optional[float]
    last_error: Optional[str]
    camera_url: str
    ingest_url: str
    has_stream_key: bool


@app.get("/api/health")
async def health() -> dict:
    """Liveness for the readiness probe. Returns 200 even when ffmpeg
    is down so the container counts as up — broadcast state lives in
    /api/status."""
    return {
        "ok": True,
        "running": _state.running,
        "has_stream_key": bool(TWITCH_STREAM_KEY),
    }


@app.get("/api/status", response_model=StatusResponse)
async def status() -> StatusResponse:
    return StatusResponse(
        running=_state.running,
        want_running=_state.want_running,
        started_at=_state.started_at,
        last_error=_state.last_error,
        camera_url=CAMERA_UPSTREAM_URL,
        ingest_url=TWITCH_INGEST_URL,
        has_stream_key=bool(TWITCH_STREAM_KEY),
    )


@app.post("/api/start")
async def start() -> dict:
    if not TWITCH_STREAM_KEY:
        raise HTTPException(503, "TWITCH_STREAM_KEY not configured")
    if not CAMERA_UPSTREAM_URL:
        raise HTTPException(503, "CAMERA_UPSTREAM_URL not configured")
    await _start()
    return {"ok": True, "running": _state.running}


@app.post("/api/stop")
async def stop() -> dict:
    await _stop()
    return {"ok": True, "running": _state.running}


def main() -> None:
    import argparse
    import uvicorn

    # The Wendy app config schema doesn't carry env vars, so we accept
    # the stream key as a CLI arg too — passed via `wendy run
    # --user-args=--twitch-stream-key=...`. The env var still wins when
    # set; the arg is the fallback. Setting os.environ before uvicorn.run
    # works because uvicorn re-imports `main:app` and the module-level
    # constants pick up the updated environment.
    p = argparse.ArgumentParser(prog="go2-broadcaster")
    p.add_argument(
        "--twitch-stream-key",
        default=None,
        help="Twitch stream key. Overrides TWITCH_STREAM_KEY env when env is unset.",
    )
    args, _ = p.parse_known_args()
    if args.twitch_stream_key and not os.environ.get("TWITCH_STREAM_KEY", "").strip():
        os.environ["TWITCH_STREAM_KEY"] = args.twitch_stream_key.strip()

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
