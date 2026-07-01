# syntax=docker/dockerfile:1.6
# go2-inspect — open-vocabulary object inspection over the Go2's front camera.
#
# Same slim FastAPI + WebRTC + from-source CycloneDDS base as go2-rc's camera
# service (proven, layer-cached), plus YOLOE (ultralytics) for open-vocab
# detection. Weights + the CLIP/MobileCLIP text backend are baked at build
# time so nothing is downloaded at runtime (container DNS may not be up).
FROM python:3.11-slim

ARG DEBIAN_FRONTEND=noninteractive

# OpenCV headless needs a few codec libs. libsrtp2-1 + libffi-dev are aiortc
# wheel deps. gcc + portaudio19-dev: unitree_webrtc_connect drags in pyaudio
# (no aarch64 wheel → built from source). cmake + build-essential + git:
# cyclonedds-python has no aarch64 wheel either → built from the C library
# installed just below.
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

# Bake the YOLOE weights AND the MobileCLIP text backend into the image.
# The first get_text_pe() call downloads yoloe-11s-seg.pt (into the target
# path) and mobileclip_blt (into ultralytics' cache); doing it here means a
# runtime scan never needs the network or the (large) text backend on disk
# afterwards. YOLOE_ALLOW_DOWNLOAD lets load_model bootstrap the weights.
ENV YOLOE_WEIGHTS=/app/weights/yoloe-11s-seg.pt
RUN --mount=type=cache,target=/root/.cache/pip \
    mkdir -p /app/weights \
    && YOLOE_ALLOW_DOWNLOAD=1 python -c "from ultralytics import YOLOE; \
m = YOLOE('/app/weights/yoloe-11s-seg.pt'); m.get_text_pe(['object'])"

COPY main.py detector.py report.py cyclonedds.xml ./
COPY static ./static

ENV PYTHONUNBUFFERED=1
ENV GO2_IP={{.GO2_IP}}
ENV PORT={{.INSPECT_PORT}}
ENV JPEG_QUALITY=80
ENV YOLO_IMGSZ=640
# Annotated-stream inference cap. On CPU, keep this low — capture (on-demand,
# full-quality) is the real workflow; the annotated stream is a live preview.
ENV YOLO_MAX_FPS=2
# CycloneDDS NIC binding (see cyclonedds.xml). Harmless if unused — this
# service only pulls video over WebRTC, but the base stack references it.
ENV CYCLONEDDS_URI=file:///app/cyclonedds.xml
# Captures + report land here. Point at a /data volume so they survive a
# container restart; main.py falls back to /app/captures if it's unwritable.
ENV CAPTURE_DIR=/data/{{.APP_ID}}/captures

EXPOSE {{.INSPECT_PORT}}

CMD ["python", "main.py"]
