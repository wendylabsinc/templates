#!/usr/bin/env python3
"""
Qwen3 4B chat backend.
Uses mlx-lm on macOS (Metal) or transformers on Linux (CUDA/CPU).
"""
import logging
import platform
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

IS_MACOS = platform.system() == "Darwin"
MLX_MODEL = "Qwen/Qwen3-4B-MLX-4bit"
HF_MODEL = "Qwen/Qwen3-4B"

model = None
tokenizer = None
_backend = None


def _load_mlx():
    global model, tokenizer, _backend
    from mlx_lm import load

    logger.info(f"Loading {MLX_MODEL} (mlx-lm / Metal) …")
    model, tokenizer = load(MLX_MODEL)
    _backend = "mlx"


def _load_transformers():
    global model, tokenizer, _backend
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    logger.info(f"Loading {HF_MODEL} (transformers) …")
    tokenizer = AutoTokenizer.from_pretrained(HF_MODEL)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    logger.info(f"Device: {device}, dtype: {dtype}")

    model = AutoModelForCausalLM.from_pretrained(
        HF_MODEL,
        torch_dtype=dtype,
        device_map="auto" if device == "cuda" else None,
    )
    if device == "cpu":
        model = model.to(device)

    _backend = "transformers"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if IS_MACOS:
        _load_mlx()
    else:
        _load_transformers()
    logger.info("Model ready.")
    yield


app = FastAPI(lifespan=lifespan)

DIST_DIR = Path(__file__).parent / "frontend" / "dist"


class ChatRequest(BaseModel):
    messages: list[dict]
    system: str | None = None
    maxTokens: int = 256
    temperature: float = 0.6
    topP: float = 0.95
    topK: int = 20


def _generate_mlx(prompt: str, req: ChatRequest) -> str:
    from mlx_lm import generate
    from mlx_lm.generate import make_sampler

    sampler = make_sampler(temp=req.temperature, top_p=req.topP, top_k=req.topK)
    return generate(
        model, tokenizer, prompt=prompt,
        max_tokens=req.maxTokens, sampler=sampler, verbose=False,
    )


def _generate_transformers(prompt: str, req: ChatRequest) -> str:
    import torch

    inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
    with torch.no_grad():
        output = model.generate(
            **inputs,
            max_new_tokens=req.maxTokens,
            temperature=req.temperature,
            top_p=req.topP,
            top_k=req.topK,
            do_sample=req.temperature > 0,
        )
    new_tokens = output[0][inputs["input_ids"].shape[1]:]
    return tokenizer.decode(new_tokens, skip_special_tokens=True)


@app.post("/api/chat")
async def chat(req: ChatRequest):
    conversation = []
    if req.system:
        conversation.append({"role": "system", "content": req.system})
    conversation.extend(req.messages)

    prompt = tokenizer.apply_chat_template(
        conversation, tokenize=False, add_generation_prompt=True
    )

    t0 = time.perf_counter()
    if _backend == "mlx":
        reply = _generate_mlx(prompt, req)
    else:
        reply = _generate_transformers(prompt, req)
    duration_ms = int((time.perf_counter() - t0) * 1000)

    # Strip <think>...</think> reasoning block from Qwen3 output
    reply = re.sub(r"<think>.*?</think>\s*", "", reply, flags=re.DOTALL).strip()

    model_name = MLX_MODEL if _backend == "mlx" else HF_MODEL
    return {"reply": reply, "model": model_name, "durationMs": duration_ms}


@app.get("/status")
async def status():
    return {
        "model": MLX_MODEL if _backend == "mlx" else HF_MODEL,
        "backend": _backend,
        "loaded": model is not None,
    }


@app.get("/logo.svg")
async def logo():
    return FileResponse(Path(__file__).parent / "logo.svg", media_type="image/svg+xml")


@app.get("/{path:path}")
async def spa(request: Request, path: str):
    """Serve the React SPA — try the exact file, fall back to index.html."""
    file = DIST_DIR / path
    if path and file.is_file():
        return FileResponse(file)
    return FileResponse(DIST_DIR / "index.html", media_type="text/html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port={{.PORT}})
