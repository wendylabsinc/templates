"""go2-RC — minimal teleop UI for the Go2.

Camera feed (proxied from a sibling MJPEG source — realsense by
default) plus virtual joystick / WASD controls that feed go2-motion's
HTTP API. Stop button hits go2-motion's /stop directly. Designed to be
the second container alongside go2-motion (and optionally realsense)
on the dog's host network — both reachable on 127.0.0.1.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("go2-RC")


MOTION_URL = os.environ.get("GO2_MOTION_URL", "http://127.0.0.1:3201").rstrip("/")
CAMERA_UPSTREAM_URL = os.environ.get(
    "CAMERA_UPSTREAM_URL", "http://127.0.0.1:8000/stream/color"
).strip()
# Same go2-camera origin as the camera stream — used for one-shot
# audio commands (bark) that need to hit a non-WS endpoint.
CAMERA_HTTP_BASE = os.environ.get(
    "CAMERA_HTTP_BASE", "http://127.0.0.1:8000"
).rstrip("/")
PORT = int(os.environ.get("PORT", "3500"))
# Tight default — teleop is latency-sensitive. Bump via env if the dog's
# wired link is flaky.
MOTION_TIMEOUT_S = float(os.environ.get("GO2_MOTION_TIMEOUT", "2.0"))

STATIC_DIR = Path(__file__).parent / "static"


# Two clients: motion gets a tight timeout and short connection pool;
# camera streams indefinitely so it gets its own client with no read
# timeout. Sharing one client makes the camera proxy time out under the
# motion timeout.
_motion_client: httpx.AsyncClient | None = None
_camera_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _motion_client, _camera_client
    _motion_client = httpx.AsyncClient(timeout=MOTION_TIMEOUT_S)
    # `timeout=None` for streaming. Connect timeout is enforced
    # separately so a dead camera doesn't hang the request forever.
    _camera_client = httpx.AsyncClient(
        timeout=httpx.Timeout(None, connect=5.0)
    )
    logger.info(
        "go2-RC up. motion=%s camera=%s port=%d",
        MOTION_URL,
        CAMERA_UPSTREAM_URL or "(none)",
        PORT,
    )
    try:
        yield
    finally:
        if _motion_client is not None:
            await _motion_client.aclose()
        if _camera_client is not None:
            await _camera_client.aclose()


app = FastAPI(title="go2-RC", lifespan=lifespan)


class VelocityBody(BaseModel):
    vx: float = Field(0.0)
    vy: float = Field(0.0)
    vyaw: float = Field(0.0)


class MoveBody(BaseModel):
    vx: float = Field(0.0)
    vy: float = Field(0.0)
    vyaw: float = Field(0.0)
    duration: float = Field(1.0)


async def _motion_post(path: str, json: dict | None = None) -> dict:
    assert _motion_client is not None
    try:
        r = await _motion_client.post(f"{MOTION_URL}{path}", json=json)
    except httpx.HTTPError as exc:
        # Mask network errors as 502 so the UI can show "motion
        # unreachable" without us leaking httpx internals.
        raise HTTPException(502, f"go2-motion unreachable: {exc}") from exc
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}


async def _motion_get(path: str) -> dict:
    assert _motion_client is not None
    try:
        r = await _motion_client.get(f"{MOTION_URL}{path}")
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"go2-motion unreachable: {exc}") from exc
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}


@app.get("/api/health")
async def health() -> dict:
    """Liveness for go2-RC + a probe of go2-motion underneath. Returns
    200 even if motion is down so the frontend can render a degraded
    state instead of failing to load."""
    motion_ok = False
    motion_reason: str | None = None
    try:
        m = await _motion_get("/health")
        motion_ok = bool(m.get("ok", False))
        if not motion_ok:
            motion_reason = m.get("reason") or "not_ready"
    except HTTPException as exc:
        motion_reason = exc.detail
    return {
        "ok": True,
        "motion": {"ok": motion_ok, "reason": motion_reason, "url": MOTION_URL},
        "camera": {"url": CAMERA_UPSTREAM_URL or None},
    }


@app.get("/api/state")
async def state() -> dict:
    return await _motion_get("/state")


@app.post("/api/velocity")
async def velocity(body: VelocityBody) -> dict:
    return await _motion_post("/velocity", body.model_dump())


@app.post("/api/move")
async def move(body: MoveBody) -> dict:
    return await _motion_post("/move", body.model_dump())


@app.post("/api/stop")
async def stop() -> dict:
    return await _motion_post("/stop")


# Each of these is a one-shot SportClient gesture on the motion side.
# Listed explicitly (not collapsed into a generic /api/skill/{name}) so
# the OpenAPI surface and the UI's button list stay aligned.
@app.post("/api/stand")
async def stand() -> dict:
    return await _motion_post("/stand")


@app.post("/api/sit")
async def sit() -> dict:
    return await _motion_post("/sit")


@app.post("/api/lie")
async def lie() -> dict:
    return await _motion_post("/lie")


@app.post("/api/hello")
async def hello() -> dict:
    return await _motion_post("/hello")


@app.post("/api/dance")
async def dance() -> dict:
    return await _motion_post("/dance")


@app.post("/api/bark")
async def bark() -> dict:
    """Trigger a synthesized bark on the dog's speaker (via go2-camera)."""
    assert _camera_client is not None
    try:
        r = await _camera_client.post(
            f"{CAMERA_HTTP_BASE}/api/bark", timeout=5.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"bark upstream unreachable: {exc}") from exc
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return r.json()


@app.get("/api/camera")
async def camera() -> StreamingResponse:
    """Transparent MJPEG passthrough.

    We proxy (rather than letting the browser hit the camera URL
    directly) so the UI works behind any single-port forward of go2-RC,
    and so a missing camera renders a clean 503 in the same origin
    instead of a CORS / mixed-content failure.
    """
    if not CAMERA_UPSTREAM_URL:
        raise HTTPException(503, "No camera configured (CAMERA_UPSTREAM_URL unset)")
    assert _camera_client is not None

    try:
        # Open the upstream stream WITHOUT awaiting the body — we hand
        # the chunks straight to the browser as they arrive.
        upstream = await _camera_client.send(
            _camera_client.build_request("GET", CAMERA_UPSTREAM_URL),
            stream=True,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"camera upstream unreachable: {exc}") from exc

    if upstream.status_code >= 400:
        body = await upstream.aread()
        await upstream.aclose()
        raise HTTPException(upstream.status_code, body.decode("utf-8", "replace"))

    media_type = upstream.headers.get(
        "content-type", "multipart/x-mixed-replace; boundary=frame"
    )

    async def relay() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(relay(), media_type=media_type)


# go2-camera now exposes a perception WebSocket alongside its MJPEG
# stream. We proxy it through the same origin as the rest of the UI so
# the browser doesn't have to deal with a second hostname/port and
# so a missing perception endpoint becomes a clean 502 instead of a
# CORS or mixed-content failure.
PERCEPTION_UPSTREAM_WS = os.environ.get(
    "PERCEPTION_UPSTREAM_WS", "ws://127.0.0.1:8000/ws/perception"
).strip()
TALK_UPSTREAM_WS = os.environ.get(
    "TALK_UPSTREAM_WS", "ws://127.0.0.1:8000/ws/talk"
).strip()


@app.websocket("/api/talk/ws")
async def talk_ws_proxy(ws: WebSocket):
    """Browser → dog speaker. Forwards binary audio frames to go2-camera.

    The browser sends binary Int16 PCM @ 8 kHz; we just relay bytes.
    Disconnect either side → close the other.
    """
    import asyncio
    # websockets >= 13 deprecated the top-level `websockets.connect()`;
    # the new home is `websockets.asyncio.client.connect()`. Import the
    # new path first and fall back so this works on both old and new
    # versions of the package.
    try:
        from websockets.asyncio.client import connect as ws_connect  # type: ignore
    except ImportError:
        from websockets import connect as ws_connect  # type: ignore

    await ws.accept()
    if not TALK_UPSTREAM_WS:
        await ws.close(code=1011, reason="talk unconfigured")
        return
    try:
        async with ws_connect(TALK_UPSTREAM_WS) as upstream:
            async def b2u() -> None:
                try:
                    while True:
                        # Use receive() to handle both text + binary;
                        # browser sends binary but we tolerate text too.
                        m = await ws.receive()
                        if m["type"] == "websocket.disconnect":
                            return
                        if m.get("bytes") is not None:
                            await upstream.send(m["bytes"])
                        elif m.get("text") is not None:
                            await upstream.send(m["text"])
                except Exception:
                    pass

            async def u2b() -> None:
                # Upstream is push-only from the browser's POV; we don't
                # expect messages back, but drain anything we get so the
                # WebSocket doesn't backpressure.
                try:
                    async for _ in upstream:
                        pass
                except Exception:
                    pass

            await asyncio.wait(
                [asyncio.create_task(b2u()), asyncio.create_task(u2b())],
                return_when=asyncio.FIRST_COMPLETED,
            )
    except Exception as exc:
        logger.warning("talk WS proxy: %s", exc)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


@app.websocket("/api/perception/ws")
async def perception_ws_proxy(ws: WebSocket):
    """Bidirectional pipe between the browser and go2-camera's perception WS.

    Tiny handler: open both sockets, then run two coroutines that copy
    text frames in each direction. If either side closes, we cancel the
    other and close. We use `websockets.connect` directly (not httpx)
    because httpx has no first-class WS support yet.
    """
    import asyncio
    # websockets >= 13 deprecated the top-level `websockets.connect()`;
    # the new home is `websockets.asyncio.client.connect()`. Import the
    # new path first and fall back so this works on both old and new
    # versions of the package.
    try:
        from websockets.asyncio.client import connect as ws_connect  # type: ignore
    except ImportError:
        from websockets import connect as ws_connect  # type: ignore

    await ws.accept()
    if not PERCEPTION_UPSTREAM_WS:
        await ws.close(code=1011, reason="perception unconfigured")
        return
    try:
        async with ws_connect(PERCEPTION_UPSTREAM_WS) as upstream:
            async def b2u() -> None:
                try:
                    while True:
                        msg = await ws.receive_text()
                        await upstream.send(msg)
                except Exception:
                    pass

            async def u2b() -> None:
                try:
                    async for msg in upstream:
                        if isinstance(msg, bytes):
                            await ws.send_bytes(msg)
                        else:
                            await ws.send_text(msg)
                except Exception:
                    pass

            await asyncio.wait(
                [asyncio.create_task(b2u()), asyncio.create_task(u2b())],
                return_when=asyncio.FIRST_COMPLETED,
            )
    except Exception as exc:
        logger.warning("perception WS proxy: %s", exc)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# Static files at /static/* (vendored later if we add real assets).
# The SPA itself is served from / via the catch-all below.
if (STATIC_DIR / "static").is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Serves the single-page UI."""
    target = STATIC_DIR / "index.html"
    if not target.is_file():
        raise HTTPException(500, f"index.html missing at {target}")
    return FileResponse(target, media_type="text/html")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
