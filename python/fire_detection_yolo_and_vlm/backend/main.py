import faulthandler
import gc
import logging
import signal
import sys
faulthandler.enable(file=sys.stderr, all_threads=True)

# SIGTRAP (sig=5) from CUDA/PyTorch native code kills the entire process.
# Install a handler that writes directly to stderr (async-signal-safe).
# IMPORTANT: Do NOT call logging.* inside a signal handler — it can deadlock
# the event loop if the logging lock is held when the signal arrives.
def _sigtrap_handler(signum, frame):
    sys.stderr.write("SIGTRAP caught — CUDA/native assertion. Process survived.\n")
    sys.stderr.flush()

signal.signal(signal.SIGTRAP, _sigtrap_handler)

# Log GC activity to detect stop-the-world pauses
def _gc_callback(phase, info):
    if phase == "start":
        _gc_start[0] = __import__("time").monotonic()
    elif phase == "stop" and _gc_start[0]:
        elapsed = __import__("time").monotonic() - _gc_start[0]
        if elapsed > 0.5:
            logging.getLogger("detector").warning(f"GC PAUSE: {elapsed:.2f}s (gen={info.get('generation', '?')})")
        _gc_start[0] = 0

_gc_start = [0]
gc.callbacks.append(_gc_callback)

from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import concurrent.futures
import os
import urllib.request

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel
import numpy as np

import detector
from visual_search import VisualSearchAgent

_search_agent = VisualSearchAgent(detector)
import profiles

FRONTEND_DIR = Path("/app/frontend/build/client")

CHAT_ENABLED = os.environ.get("ALBERT_CHAT_ENABLED", "0").lower() in ("1", "true", "yes")
DISCOVERY_ENABLED = os.environ.get("ALBERT_DISCOVERY_ENABLED", "1").lower() in ("1", "true", "yes")

from cameras import discover_cameras, mjpeg_frames, _kill_active_stream
from store import LoroStore

if DISCOVERY_ENABLED:
    from discovery import AlbertDiscovery
    discovery = AlbertDiscovery(port=5702)
else:
    discovery = None
store = LoroStore()

if CHAT_ENABLED:
    from agent import Agent
    from camera import Camera
    from llm import llm

    camera = Camera()
    agent = Agent(store=store, camera=camera)
else:
    agent = None
    llm = None

ALBERT_USER_ID = "albert"


import threading
_logger = logging.getLogger("detector")  # use detector logger so it goes to detector.log

async def _heartbeat():
    """Log a heartbeat every 60s so we know the event loop is alive."""
    while True:
        await asyncio.sleep(60)
        _logger.info(f"HEARTBEAT: event loop alive — threads={threading.active_count()}")


async def _event_loop_watchdog():
    """Detect event loop stalls — logs a warning if a sleep(1) takes >2s."""
    while True:
        t0 = asyncio.get_event_loop().time()
        await asyncio.sleep(1.0)
        elapsed = asyncio.get_event_loop().time() - t0
        if elapsed > 2.0:
            _logger.warning(f"EVENT LOOP STALL: sleep(1) took {elapsed:.1f}s — something blocked the loop")

@asynccontextmanager
async def lifespan(app: FastAPI):
    if discovery:
        discovery.start()
    if CHAT_ENABLED:
        agent.start()
    detector.start()
    heartbeat_task = asyncio.create_task(_heartbeat())
    watchdog_task = asyncio.create_task(_event_loop_watchdog())
    yield
    watchdog_task.cancel()
    heartbeat_task.cancel()
    detector.stop()
    if CHAT_ENABLED:
        agent.stop()
    if discovery:
        discovery.stop()


app = FastAPI(title="Albert Backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class CreateConversationRequest(BaseModel):
    title: str


class SendMessageRequest(BaseModel):
    userId: str
    body: str
    generateTitle: bool = False


class UpdateTitleRequest(BaseModel):
    title: str


# -- Health --


@app.get("/health")
async def health():
    return {"status": "ok", "chat_enabled": CHAT_ENABLED}


# -- Conversations --


@app.post("/conversations")
async def create_conversation(req: CreateConversationRequest):
    return store.create_conversation(title=req.title)


@app.get("/conversations")
async def list_conversations():
    return store.list_conversations()


@app.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    conv = store.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


@app.patch("/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, req: UpdateTitleRequest):
    conv = store.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    store.update_conversation_title(conversation_id, req.title)
    return store.get_conversation(conversation_id)


# -- Messages --


@app.post("/conversations/{conversation_id}/messages")
async def send_message(conversation_id: str, req: SendMessageRequest):
    # Ensure conversation exists
    conv = store.get_conversation(conversation_id)
    if conv is None:
        store.create_conversation(title="New Conversation")

    # Store the user's message
    store.send_message(
        conversation_id=conversation_id, user_id=req.userId, body=req.body
    )

    if not CHAT_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Chat is disabled. Set ALBERT_CHAT_ENABLED=1 to enable.",
        )

    # Agent processes the message (classifies intent, responds)
    result = agent.process_message(
        conversation_id=conversation_id, user_message=req.body
    )

    # Generate title from first message if requested
    if req.generateTitle:
        generated_title = llm.generate_title(req.body)
        store.update_conversation_title(conversation_id, generated_title)
        result["generatedTitle"] = generated_title

    return result


@app.get("/conversations/{conversation_id}/messages")
async def list_messages(conversation_id: str):
    return store.list_messages(conversation_id)


# -- Cameras --


@app.get("/cameras")
async def list_cameras():
    return discover_cameras()


@app.get("/cameras/{camera_id:path}/stream")
async def camera_stream(camera_id: str):
    return StreamingResponse(
        mjpeg_frames(camera_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
        background=BackgroundTask(_kill_active_stream, camera_id),
    )


# ── Detection endpoints ───────────────────────────────────────────────────────

@app.get("/api/video-feed")
async def video_feed():
    return StreamingResponse(
        detector.video_feed_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/api/detections")
async def get_detections():
    return detector.get_detections()


@app.get("/api/crops")
async def get_crops(limit: int = 20):
    return detector.get_crops(limit=min(limit, 50))


@app.get("/api/crops/{crop_id}/frame")
async def get_crop_frame(crop_id: str):
    frame_bytes = detector.get_crop_frame(crop_id)
    if not frame_bytes:
        return JSONResponse(status_code=404, content={"error": "frame not found"})
    from fastapi.responses import Response
    return Response(content=frame_bytes, media_type="image/jpeg")


@app.get("/api/crops/config")
async def get_crop_config():
    return detector.get_crop_config()


@app.post("/api/crops/config")
async def set_crop_config(body: dict):
    conf = body.get("conf_threshold")
    if conf is None:
        return JSONResponse(status_code=400, content={"error": "conf_threshold required"})
    return detector.set_crop_config(conf_threshold=conf)


@app.get("/api/profiles")
async def list_profiles():
    return profiles.list_profiles()


@app.get("/api/profiles/active")
async def get_active_profile():
    p = profiles.get_active_profile()
    if not p:
        return JSONResponse(status_code=404, content={"error": "no active profile"})
    return p


@app.post("/api/profiles/switch")
async def switch_profile(body: dict):
    profile_id = body.get("id", "").strip()
    if not profiles._model_available(
        next((p["model"] for p in profiles.PROFILES if p["id"] == profile_id), "")
    ):
        return JSONResponse(status_code=400, content={"error": f"Model not available for profile: {profile_id}"})
    profile = profiles.set_active_profile(profile_id)
    if not profile:
        return JSONResponse(status_code=404, content={"error": f"unknown profile: {profile_id}"})

    # Apply all profile settings
    # 1. Switch YOLO model
    model_path = profile["model"]
    model_result = await asyncio.get_event_loop().run_in_executor(
        None, detector.switch_to_model, model_path
    )

    # 2. Set YOLO confidence
    detector.set_conf(profile["yolo_conf"])

    # 3. Replace VLM questions
    for q in detector.get_vlm_questions():
        detector.remove_vlm_question(q["id"])
    for question in profile["vlm_questions"]:
        detector.add_vlm_question(question)

    # 4. Set VLM config
    detector.set_vlm_config(
        interval=profile["vlm_interval"],
        conf_threshold=profile["vlm_conf_threshold"],
    )

    # 5. Set crop config
    detector.set_crop_config(conf_threshold=profile["crop_conf_threshold"])

    return {"profile": profile_id, "model": model_result}


@app.get("/api/classes")
async def get_classes():
    return detector.get_classes()


@app.get("/api/hw")
async def hardware_stats():
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, detector.hardware_stats)


@app.get("/api/pause")
async def get_pause():
    return {"paused": detector.get_pause_state()}


@app.post("/api/pause")
async def toggle_pause():
    return {"paused": detector.toggle_pause()}


@app.get("/api/vlm/questions")
async def get_vlm_questions():
    return detector.get_vlm_questions()


@app.post("/api/vlm/questions")
async def add_vlm_question(body: dict):
    question = body.get("question", "").strip()
    if not question:
        return JSONResponse(status_code=400, content={"error": "question required"})
    return detector.add_vlm_question(question)


@app.delete("/api/vlm/questions/{qid}")
async def remove_vlm_question(qid: str):
    if not detector.remove_vlm_question(qid):
        return JSONResponse(status_code=404, content={"error": "not found"})
    return {"removed": qid}


@app.get("/api/vlm/config")
async def get_vlm_config():
    return detector.get_vlm_config()


@app.post("/api/vlm/config")
async def set_vlm_config(body: dict):
    return detector.set_vlm_config(
        interval=body.get("interval"),
        conf_threshold=body.get("conf_threshold"),
        classes=body.get("classes"),
    )


@app.get("/api/vlm/answers")
async def get_vlm_answers(limit: int = 50):
    return detector.get_vlm_answers(limit=min(limit, 100))


@app.post("/api/vlm/ask")
async def vlm_ask_once(body: dict):
    question = body.get("question", "").strip()
    if not question:
        return JSONResponse(status_code=400, content={"error": "question required"})
    try:
        include_image = body.get("include_image", False)
        return await detector.vlm_ask_once(question, include_image=include_image)
    except Exception as exc:
        return JSONResponse(status_code=503, content={"error": str(exc)})


@app.get("/api/camera")
async def get_camera():
    return {
        "active": detector.get_camera_index(),
        "available": detector.list_camera_indices(),
    }


@app.post("/api/camera")
async def set_camera(body: dict):
    index = body.get("index")
    if index is None:
        return JSONResponse(status_code=400, content={"error": "index required"})
    new_index = await asyncio.get_event_loop().run_in_executor(
        None, detector.set_camera_index, int(index)
    )
    return {"active": new_index}


@app.get("/api/camera/controls")
async def get_camera_controls():
    return detector.get_camera_controls()


@app.post("/api/camera/controls")
async def set_camera_control(body: dict):
    name = body.get("name")
    value = body.get("value")
    if name is None or value is None:
        return JSONResponse(status_code=400, content={"error": "name and value required"})
    return detector.set_camera_control(name, float(value))


@app.get("/api/zoom")
async def get_zoom():
    return detector.get_software_zoom()


@app.post("/api/zoom")
async def set_zoom(body: dict):
    return detector.set_software_zoom(
        zoom=body.get("zoom"),
        pan=body.get("pan"),
        tilt=body.get("tilt"),
    )


@app.post("/api/search")
async def start_search(body: dict):
    question = body.get("question", "").strip()
    if not question:
        return JSONResponse(status_code=400, content={"error": "question required"})
    return _search_agent.start_search(question)


@app.delete("/api/search")
async def stop_search():
    return _search_agent.stop_search()


@app.get("/api/search")
async def get_search_status(include_image: bool = False):
    return _search_agent.get_status(include_image=include_image)


def _find_log_path():
    """Find the detector log file — prefer /logs/ (persistent volume) over local."""
    from pathlib import Path
    p = Path("/logs/detector.log")
    if p.exists():
        return p
    p = Path(__file__).parent / "logs" / "detector.log"
    if p.exists():
        return p
    return None


@app.get("/api/logs")
async def stream_logs(lines: int = 100):
    """Return recent detector logs."""
    log_path = _find_log_path()
    if not log_path:
        return {"lines": []}
    with open(log_path) as f:
        all_lines = f.readlines()
    return {"lines": [l.rstrip() for l in all_lines[-lines:]]}


@app.get("/api/logs/vlm")
async def get_vlm_logs(lines: int = 100, minutes: int = 0):
    """Return recent VLM-related log lines. Use minutes=N to filter to last N minutes."""
    from datetime import datetime, timedelta
    log_path = _find_log_path()
    if not log_path:
        return {"lines": []}
    with open(log_path) as f:
        all_lines = f.readlines()
    vlm_lines = [l.rstrip() for l in all_lines if "VLM" in l or "vlm" in l or "VisualSearch" in l]
    if minutes > 0:
        cutoff = datetime.now() - timedelta(minutes=minutes)
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M")
        vlm_lines = [l for l in vlm_lines if l[:16] >= cutoff_str]
    return {"lines": vlm_lines[-lines:]}


@app.get("/api/logs/hw")
async def get_hw_logs(lines: int = 20):
    """Return recent hardware metric log lines."""
    log_path = _find_log_path()
    if not log_path:
        return {"lines": []}
    with open(log_path) as f:
        all_lines = f.readlines()
    hw_lines = [l.rstrip() for l in all_lines if "HW:" in l]
    return {"lines": hw_lines[-lines:]}


@app.get("/api/logs/stream")
async def stream_logs_sse():
    """SSE stream of new log lines."""
    import asyncio

    log_path = _find_log_path()

    async def _stream():
        try:
            with open(log_path) as f:
                f.seek(0, 2)  # seek to end
                while True:
                    line = f.readline()
                    if line:
                        yield f"data: {line.rstrip()}\n\n"
                    else:
                        await asyncio.sleep(0.5)
        except Exception as exc:
            yield f"data: ERROR: {exc}\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


@app.get("/api/fps")
async def get_fps():
    return {"target_fps": detector.get_target_fps()}


@app.post("/api/fps")
async def set_fps(body: dict):
    fps = body.get("target_fps")
    if fps is None:
        return JSONResponse(status_code=400, content={"error": "target_fps required"})
    return {"target_fps": detector.set_target_fps(fps)}


@app.get("/api/models")
async def get_models():
    return detector.get_models()


@app.post("/api/models/switch")
async def switch_model(path: str):
    if not Path(path).exists():
        return JSONResponse(status_code=404, content={"error": f"Model not found: {path}"})
    result = await asyncio.get_event_loop().run_in_executor(None, detector.switch_to_model, path)
    return result


@app.get("/api/models/download")
async def download_model(url: str, name: str):
    from detector import MODELS_DIR
    if not name.endswith(".pt"):
        name = name + ".pt"
    dest = MODELS_DIR / name

    async def _stream():
        try:
            yield f"data: {{"{{"}}\"status\": \"starting\", \"name\": \"{name}\"{{"}}"}}\n\n"
            MODELS_DIR.mkdir(parents=True, exist_ok=True)

            def _download():
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req) as resp, open(dest, "wb") as f:
                    total = int(resp.headers.get("Content-Length", 0))
                    downloaded = 0
                    while True:
                        buf = resp.read(65536)
                        if not buf:
                            break
                        f.write(buf)
                        downloaded += len(buf)

            loop = asyncio.get_event_loop()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = loop.run_in_executor(pool, _download)
                while not future.done():
                    await asyncio.sleep(1)
                    if dest.exists():
                        size_mb = dest.stat().st_size / 1024 / 1024
                        yield f"data: {{"{{"}}\"status\": \"downloading\", \"mb\": {size_mb:.1f}{{"}}"}}\n\n"
                await future
            yield f"data: {{"{{"}}\"status\": \"done\", \"name\": \"{name}\"{{"}}"}}\n\n"
        except Exception as e:
            if dest.exists():
                dest.unlink()
            yield f"data: {{"{{"}}\"status\": \"error\", \"message\": \"{str(e)}\"{{"}}"}}\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


@app.post("/api/infer")
async def infer_images(files: list[UploadFile] = File(...)):
    if not detector.DETECTION_AVAILABLE or detector.model is None:
        return JSONResponse(status_code=503, content={"error": "detector not ready"})
    import cv2 as _cv2
    import torch as _torch
    results_out = []
    for upload in files:
        data = await upload.read()
        arr = np.frombuffer(data, dtype=np.uint8)
        frame = _cv2.imdecode(arr, _cv2.IMREAD_COLOR)
        if frame is None:
            results_out.append({"filename": upload.filename, "error": "could not decode image"})
            continue
        with _torch.inference_mode():
            results = detector.model.predict(
                source=frame,
                device=detector.DEVICE,
                imgsz=detector.IMG_SIZE,
                half=detector.HALF,
                conf=detector.CONF,
                verbose=False,
            )
        dets = []
        for r in results:
            for box in r.boxes:
                cls_id = int(box.cls[0])
                names = detector.model.names
                cls_name = names.get(cls_id, str(cls_id)) if isinstance(names, dict) else names[cls_id]
                dets.append({
                    "label": cls_name,
                    "confidence": round(float(box.conf[0]) * 100, 1),
                    "bbox": [round(v, 1) for v in box.xyxy[0].tolist()],
                })
        annotated = results[0].plot()
        import base64 as _b64
        _, buf = _cv2.imencode(".jpg", annotated, [_cv2.IMWRITE_JPEG_QUALITY, 85])
        results_out.append({
            "filename": upload.filename,
            "detections": dets,
            "image": f"data:image/jpeg;base64,{_b64.b64encode(buf.tobytes()).decode()}",
        })
    return results_out


# ── SPA fallback ──────────────────────────────────────────────────────────────

@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    if not FRONTEND_DIR.exists():
        raise HTTPException(status_code=404, detail="Frontend not built")
    file = FRONTEND_DIR / full_path
    if file.is_file():
        return FileResponse(file)
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    raise HTTPException(status_code=404, detail="Not found")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5702)
