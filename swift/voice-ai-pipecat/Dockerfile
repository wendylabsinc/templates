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
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
ENV HF_HOME=/models/huggingface
ENV XDG_CACHE_HOME=/models/cache

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# PythonKit dlopens libpython at runtime. The multiarch lib directory differs
# between amd64 and arm64, so normalize it to a stable path.
RUN set -eu; \
    lib="$(find /usr/lib -name 'libpython3.11.so.1.0' | head -n 1)"; \
    test -n "$lib"; \
    ln -sf "$lib" /usr/local/lib/libpython3.11.so.1.0
ENV PYTHON_LIBRARY=/usr/local/lib/libpython3.11.so.1.0

# The Swift template uses the standard Swift runtime image, not JetPack's
# CUDA/cuDNN image. Keep faster-whisper on CPU so it starts consistently.
ENV WHISPER_DEVICE=cpu
ENV WHISPER_COMPUTE_TYPE=int8

ENV NLTK_DATA=/usr/local/share/nltk_data
RUN /app/venv/bin/python -c "import nltk; nltk.download('punkt_tab', download_dir='/usr/local/share/nltk_data')"

RUN mkdir -p /models/piper /models/huggingface /models/cache && \
    for triple in \
      "en/en_US/lessac/medium en_US-lessac-medium" \
      "en/en_US/ryan/high en_US-ryan-high" \
      "en/en_US/amy/medium en_US-amy-medium" \
      "en/en_GB/alan/medium en_GB-alan-medium" ; do \
        path="$(echo $triple | cut -d' ' -f1)"; \
        name="$(echo $triple | cut -d' ' -f2)"; \
        curl -fsSL -o /models/piper/${name}.onnx \
          https://huggingface.co/rhasspy/piper-voices/resolve/main/${path}/${name}.onnx && \
        curl -fsSL -o /models/piper/${name}.onnx.json \
          https://huggingface.co/rhasspy/piper-voices/resolve/main/${path}/${name}.onnx.json ; \
    done

RUN /app/venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8')"

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
