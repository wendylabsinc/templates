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
# cmake + build-essential + git are needed because cyclonedds-python has
# no aarch64 wheel either — pip builds it from source against the
# CycloneDDS C library we install just below.
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
        cmake \
        build-essential \
        git \
        pkg-config \
        ccache \
    && rm -rf /var/lib/apt/lists/*

ENV CCACHE_DIR=/ccache

# CycloneDDS C library — pinned to 0.10.5 to match cyclonedds-python<0.11.
# This is the same version go2-brain uses; staying compatible across the
# two means the same wheel/ABI combo we know works. Build is ~2-3 min on
# Orin NX, layer-cached after the first build.
ARG CYCLONEDDS_VERSION=0.10.5
RUN --mount=type=cache,target=/ccache \
    git clone --depth 1 --branch ${CYCLONEDDS_VERSION} \
        https://github.com/eclipse-cyclonedds/cyclonedds.git /tmp/cyclonedds \
    && cmake -S /tmp/cyclonedds -B /tmp/cyclonedds/build \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DBUILD_EXAMPLES=OFF \
        -DBUILD_TESTING=OFF \
        -DBUILD_IDLC=ON \
        -DCMAKE_C_COMPILER_LAUNCHER=ccache \
        -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
    && cmake --build /tmp/cyclonedds/build -j"$(nproc)" \
    && cmake --install /tmp/cyclonedds/build \
    && ldconfig \
    && rm -rf /tmp/cyclonedds

ENV CYCLONEDDS_HOME=/usr/local

WORKDIR /app

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/ccache \
    CC="ccache gcc" CXX="ccache g++" CCACHE_DIR=/ccache \
    pip install -r requirements.txt

COPY main.py perception.py audio.py webrtc_audio.py cyclonedds.xml ./

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
# CycloneDDS needs to be told which NIC carries the dog's DDS multicast
# (the same 192.168.123.0/24 subnet watchtower binds to). Without this
# the participant defaults to "any" and may route through the wrong
# interface (Docker bridge, WiFi) and never see /utlidar/cloud_deskewed.
ENV CYCLONEDDS_URI=file:///app/cyclonedds.xml

EXPOSE 8000

CMD ["python", "main.py"]
