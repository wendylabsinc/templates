from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shlex
import signal
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("llm-chat")


@dataclass(frozen=True)
class ModelPreset:
    key: str
    hf_spec: str
    min_memory_gib: int
    reason: str


MODEL_PRESETS: dict[str, ModelPreset] = {
    "nano": ModelPreset(
        key="nano",
        hf_spec="ggml-org/gemma-4-E2B-it-GGUF:Q8_0",
        min_memory_gib=0,
        reason="Gemma 4 E2B Q8 fits the smallest Orin Nano class devices.",
    ),
    "orin": ModelPreset(
        key="orin",
        hf_spec="ggml-org/gemma-4-E4B-it-GGUF:Q4_K_M",
        min_memory_gib=12,
        reason="Gemma 4 E4B Q4 is the stronger Orin-class default when memory allows.",
    ),
    "agx-orin": ModelPreset(
        key="agx-orin",
        hf_spec="ggml-org/gemma-4-26B-A4B-it-GGUF:Q4_K_M",
        min_memory_gib=48,
        reason="Gemma 4 26B A4B Q4 fits AGX Orin 64GB and favors throughput over dense 31B.",
    ),
    "thor": ModelPreset(
        key="thor",
        hf_spec="ggml-org/gemma-4-26B-A4B-it-GGUF:Q4_K_M",
        min_memory_gib=48,
        reason="Gemma 4 26B A4B Q4 is the high-memory Thor/AGX default.",
    ),
}


def _read_total_memory_gib() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                kib = int(line.split()[1])
                return kib / 1024 / 1024
    except Exception:
        return None
    return None


def _select_model() -> tuple[str, str, str]:
    requested = os.environ.get("GEMMA_MODEL", "auto").strip() or "auto"
    lowered = requested.lower()

    if lowered in MODEL_PRESETS:
        preset = MODEL_PRESETS[lowered]
        return preset.hf_spec, preset.key, preset.reason

    if lowered != "auto":
        return requested, "custom", "Custom GEMMA_MODEL was provided."

    total_gib = _read_total_memory_gib()
    if total_gib is not None and total_gib >= MODEL_PRESETS["agx-orin"].min_memory_gib:
        preset = MODEL_PRESETS["agx-orin"]
        return preset.hf_spec, "auto:agx-orin", f"{preset.reason} Detected {total_gib:.1f} GiB RAM."
    if total_gib is not None and total_gib >= MODEL_PRESETS["orin"].min_memory_gib:
        preset = MODEL_PRESETS["orin"]
        return preset.hf_spec, "auto:orin", f"{preset.reason} Detected {total_gib:.1f} GiB RAM."

    preset = MODEL_PRESETS["nano"]
    if total_gib is None:
        return preset.hf_spec, "auto:nano", f"{preset.reason} Could not detect total RAM."
    return preset.hf_spec, "auto:nano", f"{preset.reason} Detected {total_gib:.1f} GiB RAM."


SELECTED_MODEL, MODEL_PRESET, MODEL_REASON = _select_model()
LLAMA_PORT = int(os.environ.get("LLAMA_CPP_PORT", "8081"))
LLAMA_BASE_URL = os.environ.get("LLAMA_CPP_BASE_URL", f"http://127.0.0.1:{LLAMA_PORT}/v1").rstrip("/")
LLAMA_ROOT_URL = LLAMA_BASE_URL.removesuffix("/v1")
MANAGED_LLAMA = "LLAMA_CPP_BASE_URL" not in os.environ

_llama_process: subprocess.Popen[str] | None = None


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1)
    max_tokens: int = Field(default=768, ge=1, le=8192)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.95, ge=0.0, le=1.0)


def _llama_command() -> list[str]:
    cmd = [
        os.environ.get("LLAMA_SERVER_BIN", "llama-server"),
        "--host",
        "127.0.0.1",
        "--port",
        str(LLAMA_PORT),
        "-hf",
        SELECTED_MODEL,
        "-c",
        os.environ.get("LLAMA_CONTEXT_SIZE", "4096"),
        "-ngl",
        os.environ.get("LLAMA_GPU_LAYERS", "99"),
        "--threads",
        os.environ.get("LLAMA_THREADS", str(os.cpu_count() or 4)),
        "-fa",
        os.environ.get("LLAMA_FLASH_ATTN", "on"),
        "-ctk",
        os.environ.get("LLAMA_CACHE_TYPE_K", "q8_0"),
        "-ctv",
        os.environ.get("LLAMA_CACHE_TYPE_V", "q8_0"),
    ]
    extra = os.environ.get("LLAMA_CPP_EXTRA_ARGS", "").strip()
    if extra:
        cmd.extend(shlex.split(extra))
    return cmd


def _pipe_process_logs(process: subprocess.Popen[str]) -> None:
    async def pump() -> None:
        if process.stdout is None:
            return
        while True:
            line = await asyncio.to_thread(process.stdout.readline)
            if not line:
                return
            logger.info("llama.cpp: %s", line.rstrip())

    asyncio.create_task(pump())


async def _llama_healthy(timeout: float = 2.0) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{LLAMA_ROOT_URL}/health")
            return response.status_code < 500
    except httpx.HTTPError:
        return False


async def _wait_for_llama(max_seconds: float) -> bool:
    deadline = asyncio.get_running_loop().time() + max_seconds
    while asyncio.get_running_loop().time() < deadline:
        if await _llama_healthy():
            return True
        await asyncio.sleep(1)
    return False


def _start_llama() -> None:
    global _llama_process
    if not MANAGED_LLAMA:
        logger.info("Using external llama.cpp server at %s", LLAMA_BASE_URL)
        return
    if _llama_process and _llama_process.poll() is None:
        return

    Path(os.environ.get("LLAMA_CACHE", "/models/llama.cpp")).mkdir(parents=True, exist_ok=True)
    Path(os.environ.get("HF_HOME", "/models/huggingface")).mkdir(parents=True, exist_ok=True)

    cmd = _llama_command()
    logger.info("Starting llama.cpp: %s", shlex.join(cmd))
    _llama_process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    _pipe_process_logs(_llama_process)


async def _stop_llama() -> None:
    global _llama_process
    process = _llama_process
    _llama_process = None
    if process is None or process.poll() is not None:
        return

    process.send_signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=15)
    except asyncio.TimeoutError:
        process.kill()
        await asyncio.to_thread(process.wait)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    _start_llama()
    yield
    await _stop_llama()


app = FastAPI(title=os.environ.get("APP_ID", "llm-chat"), lifespan=lifespan)
_static_dir = Path(__file__).resolve().parent.parent / "static"


def _system_prompt() -> str:
    return os.environ.get(
        "SYSTEM_PROMPT",
        "You are a concise, practical assistant running locally on a WendyOS device.",
    )


async def _stream_llama_reply(request: ChatRequest) -> AsyncIterator[bytes]:
    if not await _wait_for_llama(float(os.environ.get("LLAMA_READY_WAIT_SECS", "900"))):
        yield b"llama.cpp is still starting or downloading the model. Check /api/status and try again."
        return

    messages = [message.model_dump() for message in request.messages]
    if messages[0]["role"] != "system":
        messages.insert(0, {"role": "system", "content": _system_prompt()})

    payload = {
        "model": os.environ.get("LLAMA_MODEL_ALIAS", "local-gemma-4"),
        "messages": messages,
        "stream": True,
        "temperature": request.temperature,
        "top_p": request.top_p,
        "max_tokens": request.max_tokens,
    }

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", f"{LLAMA_BASE_URL}/chat/completions", json=payload) as response:
                if response.status_code >= 400:
                    detail = await response.aread()
                    raise HTTPException(
                        status_code=502,
                        detail=f"llama.cpp returned HTTP {response.status_code}: {detail.decode(errors='replace')[:800]}",
                    )

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    content = delta.get("content") or choice.get("text") or ""
                    if content:
                        yield content.encode("utf-8")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Chat request failed")
        yield f"\n\nllm-chat backend error: {exc}".encode("utf-8")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/status")
async def status() -> dict[str, object]:
    total_gib = _read_total_memory_gib()
    return {
        "appId": os.environ.get("APP_ID", "llm-chat"),
        "backend": "llama.cpp",
        "model": {
            "requested": os.environ.get("GEMMA_MODEL", "auto"),
            "selected": SELECTED_MODEL,
            "preset": MODEL_PRESET,
            "reason": MODEL_REASON,
        },
        "llama": {
            "baseUrl": LLAMA_BASE_URL,
            "managed": MANAGED_LLAMA,
            "running": (not MANAGED_LLAMA) or (_llama_process is not None and _llama_process.poll() is None),
            "healthy": await _llama_healthy(),
        },
        "runtime": {
            "contextSize": os.environ.get("LLAMA_CONTEXT_SIZE", "4096"),
            "gpuLayers": os.environ.get("LLAMA_GPU_LAYERS", "99"),
            "threads": os.environ.get("LLAMA_THREADS", str(os.cpu_count() or 4)),
            "cacheTypeK": os.environ.get("LLAMA_CACHE_TYPE_K", "q8_0"),
            "cacheTypeV": os.environ.get("LLAMA_CACHE_TYPE_V", "q8_0"),
        },
        "system": {
            "hostname": os.environ.get("WENDY_HOSTNAME", platform.node()),
            "platform": os.environ.get("WENDY_PLATFORM", platform.system()),
            "deviceType": os.environ.get("WENDY_DEVICE_TYPE", ""),
            "architecture": platform.machine(),
            "memoryGiB": round(total_gib, 1) if total_gib is not None else None,
        },
    }


@app.post("/api/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    return StreamingResponse(_stream_llama_reply(request), media_type="text/plain; charset=utf-8")


@app.get("/{full_path:path}")
async def serve_spa(full_path: str) -> FileResponse:
    file_path = _static_dir / full_path
    if file_path.is_file():
        return FileResponse(file_path)
    return FileResponse(_static_dir / "index.html")
