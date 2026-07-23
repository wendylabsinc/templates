# syntax=docker/dockerfile:1.6
#
# CPU-only slim image (default) — works on any device, no GPU required.
# For NVIDIA Jetson devices, build with: --dockerfile Dockerfile.jetson-slim
# (requires a JetPack-matched TORCH_WHL_URL build-arg; see README-JETSON.md).
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Minimal runtime lib for PyTorch CPU wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY main.py .

# Install runtime deps (CPU-only)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system torch numpy

CMD ["python", "main.py"]
