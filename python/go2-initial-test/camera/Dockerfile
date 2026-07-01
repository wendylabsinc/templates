# syntax=docker/dockerfile:1.6
# go2-initial-test — camera test (Go2 front camera over WebRTC, one frame).
# No CycloneDDS needed (camera is WebRTC, not DDS). System libs are aiortc/
# opencv/pyaudio deps (unitree_webrtc_connect drags pyaudio → needs portaudio).
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libsrtp2-1 libffi-dev libssl-dev \
        gcc python3-dev portaudio19-dev libportaudio2 ca-certificates ccache \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip --mount=type=cache,target=/ccache \
    CC="ccache gcc" CXX="ccache g++" CCACHE_DIR=/ccache pip install -r requirements.txt
COPY app.py ./

ENV GO2_IP={{.GO2_IP}}
ENV PORT=3612
EXPOSE 3612

CMD ["python", "app.py"]
