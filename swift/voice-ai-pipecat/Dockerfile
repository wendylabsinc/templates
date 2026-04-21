# Stage 1 - Build React visualizer
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2 - Build the Swift bridge
FROM swift:{{.SWIFT_VERSION}}-bookworm AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpython3-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY Package.swift ./
COPY Sources Sources
RUN swift build -c release

# Stage 3 - Runtime image (Swift binary + CPython + Pipecat).
# Jetson Orin Nano with CUDA: swap to nvcr.io/nvidia/l4t-pytorch:r36.2.0-pth2.2-py3
# and install Swift separately via swiftly if you need GPU acceleration.
FROM swift:{{.SWIFT_VERSION}}-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    libpython3.11 \
    alsa-utils \
    libsndfile1 \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# PythonKit dlopens libpython at runtime; point it at the venv's interpreter.
ENV PYTHON_LIBRARY=/usr/lib/aarch64-linux-gnu/libpython3.11.so.1.0

COPY --from=builder /app/.build/release/{{.APP_ID}} /usr/local/bin/{{.APP_ID}}

# Python sources loaded via PythonKit (see Sources/{{.APP_ID}}/main.swift).
RUN mkdir -p /app/python
COPY main.py pipeline.py /app/python/
COPY --from=frontend /frontend/dist /app/static

ENV GOOGLE_API_KEY={{.GOOGLE_API_KEY}}
ENV PORT={{.PORT}}
ENV STATIC_DIR=/app/static
ENV PYTHONUNBUFFERED=1

EXPOSE {{.PORT}}

CMD ["{{.APP_ID}}"]
