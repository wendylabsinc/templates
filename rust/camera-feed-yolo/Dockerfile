# syntax=docker/dockerfile:1.6
#
# Multi-stage build keyed on WENDY_PLATFORM (injected by `wendy run`).
#   nvidia-jetson -> dustynv/onnxruntime (CUDA-built libonnxruntime.so)
#   generic       -> rust:1.88-slim + Microsoft prebuilt CPU onnxruntime
# Falls back to "generic" when no build arg is supplied.

ARG WENDY_PLATFORM=generic

# ── Stage A: export yolov8n.onnx ─────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS model-export
# ultralytics pulls in opencv-python (not headless), which dlopens libxcb/libgl
# at import time — slim doesn't ship them, so cv2 fails to load before export runs.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxcb1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir ultralytics onnx
WORKDIR /export
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt').export(format='onnx', imgsz=640, opset=12, simplify=True, dynamic=False)" \
 && [ -s /export/yolov8n.onnx ] \
 && [ "$(stat -c %s /export/yolov8n.onnx)" -gt 5000000 ]

# ── Stage B-jetson: CUDA onnxruntime + Rust ──────────────────────────────────
FROM dustynv/onnxruntime:1.20.0-r36.4.0 AS base-nvidia-jetson

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates pkg-config build-essential \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    v4l-utils libturbojpeg0-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Rust via rustup (dustynv's base has no Rust toolchain).
# 1.88+ required by ort's libloading 0.9 transitive dep.
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain 1.88.0 --profile minimal
ENV PATH="/root/.cargo/bin:${PATH}"

# dustynv installs onnxruntime as a Python wheel; expose libonnxruntime.so under
# /usr/local/lib so the ort crate's load-dynamic feature can dlopen it.
RUN LIB=$(find / -name "libonnxruntime.so*" 2>/dev/null | grep -v __pycache__ | sort -r | head -1) \
 && [ -n "$LIB" ] || (echo "libonnxruntime.so not found" >&2 && exit 1) \
 && ln -sf "$LIB" /usr/local/lib/libonnxruntime.so \
 && ldconfig

# ── Stage B-generic: CPU onnxruntime + Rust ──────────────────────────────────
# 1.88+ required by ort's libloading 0.9 transitive dep.
FROM rust:1.88-slim AS base-generic

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates pkg-config \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    v4l-utils libturbojpeg0-dev \
    && rm -rf /var/lib/apt/lists/*

# Microsoft prebuilt onnxruntime CPU release. asset name uses x64 / aarch64.
RUN ARCH="$(uname -m)" \
 && case "$ARCH" in \
        x86_64) ORT_ARCH=x64 ;; \
        aarch64) ORT_ARCH=aarch64 ;; \
        *) echo "unsupported arch: $ARCH" >&2; exit 1 ;; \
    esac \
 && curl -fsSL "https://github.com/microsoft/onnxruntime/releases/download/v1.20.0/onnxruntime-linux-${ORT_ARCH}-1.20.0.tgz" \
      -o /tmp/ort.tgz \
 && tar -xzf /tmp/ort.tgz -C /usr/local --strip-components=1 \
 && rm /tmp/ort.tgz \
 && ldconfig

# ── Stage C: build the app on the chosen base ────────────────────────────────
FROM base-${WENDY_PLATFORM} AS final
ARG WENDY_PLATFORM=generic
ARG WENDY_DEVICE_TYPE=""
ARG WENDY_HAS_GPU=""
ARG WENDY_GPU_VENDOR=""
ENV WENDY_PLATFORM=${WENDY_PLATFORM} \
    WENDY_DEVICE_TYPE=${WENDY_DEVICE_TYPE} \
    WENDY_HAS_GPU=${WENDY_HAS_GPU} \
    WENDY_GPU_VENDOR=${WENDY_GPU_VENDOR} \
    ORT_DYLIB_PATH=/usr/local/lib/libonnxruntime.so \
    TURBOJPEG_SOURCE=pkg-config

WORKDIR /app
COPY Cargo.toml ./
COPY src ./src

RUN cargo build --release \
 && cp target/release/{{.APP_ID}} /usr/local/bin/{{.APP_ID}} \
 && rm -rf target

COPY --from=model-export /export/yolov8n.onnx ./yolov8n.onnx
COPY index.html ./
COPY assets/ ./assets/

EXPOSE {{.PORT}}

CMD ["{{.APP_ID}}"]
