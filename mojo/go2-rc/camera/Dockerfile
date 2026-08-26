# syntax=docker/dockerfile:1.6
# Slim FastAPI image — no ROS2, no colcon. Builds in ~1-2 min on first
# run, ~30s on cached rebuilds.
FROM python:3.11-slim

ARG DEBIAN_FRONTEND=noninteractive

# OpenCV headless still needs a few system libs for video / image codecs.
# libsrtp2-1 + libffi-dev are pulled in by aiortc's wheel deps.
# gcc + portaudio19-dev are needed because unitree_webrtc_connect drags
# in pyaudio (for the audio path we don't use), and pyaudio has no
# aarch64 wheel — pip compiles it from source against PortAudio headers.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libsrtp2-1 \
        libffi-dev \
        libssl-dev \
        gcc \
        python3-dev \
        portaudio19-dev \
        libportaudio2 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

COPY main.py webrtc_audio.py ./

ENV PYTHONUNBUFFERED=1
ENV GO2_IP={{.GO2_IP}}
ENV PORT=8000
ENV JPEG_QUALITY=80
# µ-law @ 8 kHz is the canonical Unitree audio format (per
# watchtower's go2_audio_bridge.py). PCM16 / alaw were experiments to
# rule out codec issues — the actual gating step turned out to be
# enter_megaphone() in main.py. Override here if a future firmware
# expects something else.
ENV AUDIO_OUT_CODEC=ulaw
EXPOSE 8000

CMD ["python", "main.py"]
