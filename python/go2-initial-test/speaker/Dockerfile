# syntax=docker/dockerfile:1.6
# go2-initial-test — speaker test (Go2 megaphone over the WebRTC audiohub).
#
# Same WebRTC stack as the camera service (unitree_webrtc_connect → aiortc +
# pyaudio/portaudio). NO CycloneDDS — the DDS rt/audioreceiver path was
# unreliable ("pure noise" / not the live channel); the audiohub megaphone file
# API is what actually plays on the dog.
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

ENV PORT=3615
# Robot controller IP — the WebRTC target (single-client; contends with camera).
ENV GO2_IP={{.GO2_IP}}
ENV TONE_HZ=880
ENV TONE_SECONDS=1.5
EXPOSE 3615

CMD ["python", "app.py"]
