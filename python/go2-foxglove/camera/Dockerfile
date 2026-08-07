# syntax=docker/dockerfile:1.6
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libsrtp2-1 libffi-dev libssl-dev \
        gcc python3-dev portaudio19-dev libportaudio2 ca-certificates ccache \
    && rm -rf /var/lib/apt/lists/*
ENV CCACHE_DIR=/ccache

WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/ccache \
    CC="ccache gcc" CXX="ccache g++" CCACHE_DIR=/ccache \
    pip install --no-cache-dir -r requirements.txt
COPY app.py ./

ENV GO2_IP={{.GO2_IP}}
ENV BRIDGE_FRAME_URL=http://127.0.0.1:8769/frame
ENV BRIDGE_STATUS_URL=http://127.0.0.1:8769/status
ENV HEALTH_PORT=8768
ENV MAX_FPS=15
ENV JPEG_QUALITY=80
EXPOSE 8768

CMD ["python", "app.py"]
