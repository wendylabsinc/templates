# syntax=docker/dockerfile:1.6
# go2-foxglove — camera (Go2 front camera over WebRTC → JPEG → bridge).
# No CycloneDDS needed (camera is WebRTC, not DDS). System libs are aiortc/opencv/
# pyaudio deps (unitree_webrtc_connect drags pyaudio → needs portaudio).
# Build for arm64 (the Go2's Orin): `--platform linux/arm64` from an x86 host.
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
    pip install -r requirements.txt
COPY app.py ./

ENV GO2_IP={{.GO2_IP}}
# The camera forwards to the bridge over localhost (both run network: host).
ENV BRIDGE_INGEST_URL=http://127.0.0.1:8766/frame
ENV MAX_FPS=12
ENV JPEG_QUALITY=80

CMD ["python", "app.py"]
