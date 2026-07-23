# ── Stage 1: Build frontend ──────────────────────────────────────────
FROM node:22-slim AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ .
RUN npm run build

# ── Stage 2: Python runtime (transformers + torch for Linux) ─────────
FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY logo.svg .
COPY --from=frontend-build /build/dist frontend/dist

RUN useradd --create-home --shell /bin/bash app && \
    chown -R app:app /app
USER app

# Pre-download the model weights at build time
RUN python -c "from transformers import AutoModelForCausalLM, AutoTokenizer; AutoTokenizer.from_pretrained('Qwen/Qwen3-4B'); AutoModelForCausalLM.from_pretrained('Qwen/Qwen3-4B')"

EXPOSE {{.PORT}}
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "{{.PORT}}"]
