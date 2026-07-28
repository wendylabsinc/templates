"""REST + WebSocket endpoints for the sensing pipeline."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

router = APIRouter()       # mounted under /api
ws_router = APIRouter()    # mounted at root


def _pipeline(app):
    return app.state.pipeline


@router.get("/status")
def status(request: Request):
    return _pipeline(request.app).status()


@router.get("/sensors")
def sensors(request: Request):
    pipe = _pipeline(request.app)
    return [s.to_dict() for s in pipe.store.stats().values()]


@router.get("/config")
def get_config(request: Request):
    cfg = _pipeline(request.app).config
    return {
        "udp_port": cfg.udp_port,
        "analysis_rate_hz": cfg.analysis_rate_hz,
        "presence_window_s": cfg.presence_window_s,
        "vitals_window_s": cfg.vitals_window_s,
        "motion_threshold": cfg.motion_threshold,
    }


@router.post("/calibrate")
def calibrate(request: Request):
    try:
        baseline = _pipeline(request.app).calibrate()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return {"baseline": baseline}


@ws_router.websocket("/ws/stream")
async def stream(websocket: WebSocket):
    await websocket.accept()
    pipe = _pipeline(websocket.app)
    queue = pipe.subscribe()
    try:
        if pipe.latest is not None:
            await websocket.send_json(pipe.latest.to_dict())
        while True:
            frame = await queue.get()
            await websocket.send_json(frame.to_dict())
    except WebSocketDisconnect:
        pass
    finally:
        pipe.unsubscribe(queue)
