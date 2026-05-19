from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shlex
import signal
import subprocess
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from huggingface_hub import HfApi, hf_hub_download
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
_llama_start_task: asyncio.Task[None] | None = None
_llama_start_error: str | None = None
_resolved_model_path: str | None = None


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1)
    max_tokens: int = Field(default=768, ge=1, le=8192)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.95, ge=0.0, le=1.0)


def _hub_token() -> str | None:
    return os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or None


def _normalize_token(value: str) -> str:
    return value.lower().replace("-", "_")


def _parse_hf_spec(model_spec: str) -> tuple[str, str | None] | None:
    spec = model_spec.strip()
    for prefix in ("https://huggingface.co/", "https://hf.co/", "hf.co/"):
        if spec.startswith(prefix):
            spec = spec.removeprefix(prefix)
            break
    spec = spec.strip("/")

    if "/blob/" in spec:
        repo_id, path = spec.split("/blob/", 1)
        parts = path.split("/", 1)
        if len(parts) == 2 and parts[1].lower().endswith(".gguf"):
            return repo_id, parts[1]

    if ":" in spec:
        repo_id, quant = spec.rsplit(":", 1)
        if "/" in repo_id and quant:
            return repo_id, quant

    if "/" in spec and not spec.lower().endswith(".gguf"):
        return spec, None

    return None


def _select_gguf_filename(repo_id: str, selector: str | None) -> str:
    filename_override = os.environ.get("GEMMA_MODEL_FILE", "").strip()
    if filename_override:
        return filename_override

    info = HfApi(token=_hub_token()).model_info(repo_id)
    gguf_files = sorted(
        filename
        for sibling in info.siblings or []
        if (filename := getattr(sibling, "rfilename", "")).lower().endswith(".gguf")
        and not Path(filename).name.lower().startswith("mmproj-")
    )
    if not gguf_files:
        raise RuntimeError(f"No GGUF model files found in Hugging Face repo {repo_id!r}.")

    if selector and selector.lower().endswith(".gguf"):
        if selector in gguf_files:
            return selector
        raise RuntimeError(f"GGUF file {selector!r} was not found in Hugging Face repo {repo_id!r}.")

    if selector:
        normalized_selector = _normalize_token(selector)
        matches = [name for name in gguf_files if normalized_selector in _normalize_token(name)]
        if not matches:
            raise RuntimeError(
                f"No GGUF file matching quantization {selector!r} was found in Hugging Face repo {repo_id!r}."
            )
        return sorted(matches, key=lambda name: (len(Path(name).name), name))[0]

    preferred_quantizations = ("q4_k_m", "q8_0", "q6_k", "q5_k_m", "q4_k_s")
    for quantization in preferred_quantizations:
        matches = [name for name in gguf_files if quantization in _normalize_token(name)]
        if matches:
            return sorted(matches, key=lambda name: (len(Path(name).name), name))[0]

    return sorted(gguf_files, key=lambda name: (len(Path(name).name), name))[0]


def _resolve_model_path(model_spec: str) -> str:
    model_path = os.environ.get("LLAMA_MODEL_PATH", "").strip()
    if model_path:
        return model_path

    preloaded_model_path = os.environ.get(
        "PRELOADED_GEMMA_MODEL_PATH",
        "/opt/wendy/models/gemma-4-E2B-it-Q8_0.gguf",
    )
    if model_spec == MODEL_PRESETS["nano"].hf_spec and Path(preloaded_model_path).is_file():
        logger.info("Using preloaded Gemma 4 Nano model at %s", preloaded_model_path)
        return preloaded_model_path

    path = Path(model_spec)
    if path.is_absolute() or model_spec.startswith(".") or model_spec.lower().endswith(".gguf"):
        return model_spec

    hf_spec = _parse_hf_spec(model_spec)
    if hf_spec is None:
        return model_spec

    repo_id, selector = hf_spec
    filename = _select_gguf_filename(repo_id, selector)
    logger.info("Downloading/resolving Hugging Face GGUF model %s/%s", repo_id, filename)
    return hf_hub_download(repo_id=repo_id, filename=filename, token=_hub_token())


def _llama_command(model_path: str) -> list[str]:
    cmd = [
        os.environ.get("LLAMA_SERVER_BIN", "llama-server"),
        "--host",
        "127.0.0.1",
        "--port",
        str(LLAMA_PORT),
        "-m",
        model_path,
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
    reasoning = os.environ.get("LLAMA_REASONING", "off").strip()
    if reasoning:
        cmd.extend(["--reasoning", reasoning])

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
        if _llama_start_error:
            return False
        if _llama_process is not None and _llama_process.poll() is not None:
            return False
        if await _llama_healthy():
            return True
        await asyncio.sleep(1)
    return False


async def _start_llama() -> None:
    global _llama_process, _llama_start_error, _resolved_model_path
    if not MANAGED_LLAMA:
        logger.info("Using external llama.cpp server at %s", LLAMA_BASE_URL)
        return
    if _llama_process and _llama_process.poll() is None:
        return

    _llama_start_error = None
    try:
        Path(os.environ.get("LLAMA_CACHE", "/models/llama.cpp")).mkdir(parents=True, exist_ok=True)
        Path(os.environ.get("HF_HOME", "/models/huggingface")).mkdir(parents=True, exist_ok=True)
        model_path = await asyncio.to_thread(_resolve_model_path, SELECTED_MODEL)
        _resolved_model_path = model_path

        cmd = _llama_command(model_path)
        logger.info("Starting llama.cpp: %s", shlex.join(cmd))
        _llama_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        _pipe_process_logs(_llama_process)
    except Exception as exc:
        _llama_start_error = str(exc)
        logger.exception("Failed to start llama.cpp")


def _schedule_llama_start() -> None:
    global _llama_start_task
    if not MANAGED_LLAMA:
        return
    if _llama_start_task is not None and not _llama_start_task.done():
        return
    if _llama_process and _llama_process.poll() is None:
        return
    _llama_start_task = asyncio.create_task(_start_llama())


async def _stop_llama() -> None:
    global _llama_process, _llama_start_task
    task = _llama_start_task
    _llama_start_task = None
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

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
    _schedule_llama_start()
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
    _schedule_llama_start()
    if _llama_start_error:
        yield f"llama.cpp failed to start: {_llama_start_error}".encode("utf-8")
        return
    if not await _wait_for_llama(float(os.environ.get("LLAMA_READY_WAIT_SECS", "900"))):
        if _llama_start_error:
            yield f"llama.cpp failed to start: {_llama_start_error}".encode("utf-8")
            return
        if _llama_process is not None and _llama_process.poll() is not None:
            yield f"llama.cpp exited with code {_llama_process.poll()} before becoming healthy.".encode("utf-8")
            return
        yield b"llama.cpp is still starting or downloading the model. Check /api/status and try again."
        return

    messages = [message.model_dump() for message in request.messages]
    if messages[0]["role"] != "system":
        messages.insert(0, {"role": "system", "content": _system_prompt()})

    payload = {
        "model": os.environ.get("LLAMA_MODEL_ALIAS", "local-gemma-4"),
        "messages": messages,
        "stream": False,
        "temperature": request.temperature,
        "top_p": request.top_p,
        "max_tokens": request.max_tokens,
    }

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            response = await client.post(f"{LLAMA_BASE_URL}/chat/completions", json=payload)
            if response.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"llama.cpp returned HTTP {response.status_code}: {response.text[:800]}",
                )

            chunk = response.json()
            choice = (chunk.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = message.get("content") or choice.get("text") or ""
            if content:
                yield content.encode("utf-8")
            else:
                detail = json.dumps(chunk)[:800]
                logger.warning("llama.cpp returned no text content: %s", detail)
                yield f"llama.cpp returned no text content: {detail}".encode("utf-8")
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
            "resolvedPath": _resolved_model_path,
            "preset": MODEL_PRESET,
            "reason": MODEL_REASON,
        },
        "llama": {
            "baseUrl": LLAMA_BASE_URL,
            "managed": MANAGED_LLAMA,
            "starting": _llama_start_task is not None and not _llama_start_task.done(),
            "running": (not MANAGED_LLAMA) or (_llama_process is not None and _llama_process.poll() is None),
            "exitCode": None if _llama_process is None else _llama_process.poll(),
            "healthy": await _llama_healthy(),
            "error": _llama_start_error,
        },
        "runtime": {
            "contextSize": os.environ.get("LLAMA_CONTEXT_SIZE", "4096"),
            "gpuLayers": os.environ.get("LLAMA_GPU_LAYERS", "99"),
            "threads": os.environ.get("LLAMA_THREADS", str(os.cpu_count() or 4)),
            "reasoning": os.environ.get("LLAMA_REASONING", "off"),
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
