# syntax=docker/dockerfile:1.6
#
# Multi-stage build keyed on WENDY_PLATFORM.
#   nvidia-jetson -> dustynv/onnxruntime + Node 22. We swap onnxruntime-node's
#                    bundled CPU-only libonnxruntime.so.1 for dustynv's
#                    CUDA-enabled build so the CUDA EP is actually available.
#   generic       -> node:22-slim + Microsoft prebuilt CPU onnxruntime.
#
# onnxruntime-node is pinned to ~1.20.0 in package.json so the bundled binding
# stays ABI-compatible with the dustynv 1.20.x libonnxruntime.so we swap in.

ARG WENDY_PLATFORM=generic

# ── Stage A: export yolov8n.onnx ─────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS model-export
# ultralytics pulls in opencv-python (not headless), which dlopens libxcb/libgl
# at import time — slim doesn't ship them, so cv2 fails to load before export runs.
RUN --mount=type=cache,target=/root/.cache/pip \
    apt-get update && apt-get install -y --no-install-recommends \
    libxcb1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
 && pip install ultralytics onnx
WORKDIR /export
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt').export(format='onnx', imgsz=640, opset=12, simplify=True, dynamic=False)" \
 && [ -s /export/yolov8n.onnx ] \
 && [ "$(stat -c %s /export/yolov8n.onnx)" -gt 5000000 ]

# ── Stage B-jetson ──────────────────────────────────────────────────────────
FROM dustynv/onnxruntime:1.20.2-r36.4.0 AS base-nvidia-jetson

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    v4l-utils \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# ── JetPack 7 / WendyOS 0.17 GPU fix (WendyOS#1370; mirrors WendyOS PR #1379) ──
# This dustynv base targets JetPack 6 (CUDA 12.x + cuDNN 9). On a JetPack-7 Orin
# the host injects CUDA 13, and the base's own default LD_LIBRARY_PATH lists
# /usr/local/cuda/compat (a bundled CUDA-12 forward-compat *driver*) first, which
# shadows the host's real CUDA-13 driver and dies at cuInit with Error 801 — the
# GPU vanishes and inference silently falls back to CPU. Fix: gather the image's
# CUDA-12 *runtime* libs (toolkit under targets/*/lib + cuDNN in the multiarch
# dir) into one dir, put it first on the loader path, and drop the compat dir so
# the host CUDA-13 driver is used (CUDA 12 runs on it via backward compat, and the
# CUDA-12 build ships the Orin sm_87 kernels the CUDA-13 sbsa build lacks). Never
# bundle libcuda.so* — the GPU driver must stay the CDI/host one.
RUN mkdir -p /opt/cuda12/lib \
 && find /usr/local/cuda/targets/*/lib /usr/lib/aarch64-linux-gnu -maxdepth 1 \( \
        -name 'libcudart.so*'  -o -name 'libcublas*.so*'   -o -name 'libcufft*.so*'   \
     -o -name 'libcurand.so*'  -o -name 'libcusolver*.so*' -o -name 'libcusparse.so*' \
     -o -name 'libnvrtc*.so*'  -o -name 'libnpp*.so*'      -o -name 'libcudnn*.so*'   \
   \) ! -name 'libcuda.so*' -exec ln -sf {} /opt/cuda12/lib/ \; 2>/dev/null \
 && echo /opt/cuda12/lib > /etc/ld.so.conf.d/000-cuda12.conf \
 && ldconfig
ENV LD_LIBRARY_PATH=/opt/cuda12/lib

# ── Stage B-generic ─────────────────────────────────────────────────────────
FROM node:22-slim AS base-generic

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    v4l-utils \
    && rm -rf /var/lib/apt/lists/*

# ── Stage C: build app + final image ────────────────────────────────────────
FROM base-${WENDY_PLATFORM} AS final
ARG WENDY_PLATFORM=generic
ARG WENDY_DEVICE_TYPE=""
ARG WENDY_HAS_GPU=""
ARG WENDY_GPU_VENDOR=""
ENV WENDY_PLATFORM=${WENDY_PLATFORM} \
    WENDY_DEVICE_TYPE=${WENDY_DEVICE_TYPE} \
    WENDY_HAS_GPU=${WENDY_HAS_GPU} \
    WENDY_GPU_VENDOR=${WENDY_GPU_VENDOR}

WORKDIR /app
COPY package.json package-lock.json* ./
RUN --mount=type=cache,target=/root/.npm npm install

# On Jetson, replace the npm-bundled libonnxruntime.so.1 (arm64, CPU-only —
# NuGet has no CUDA EP package for linux/arm64) with a symlink to dustynv's
# CUDA-enabled build. The symlink (rather than a copy) preserves the original
# location so the runtime's $ORIGIN-relative RPATH can still locate
# libonnxruntime_providers_cuda.so next to the real file. Safe because
# package.json pins onnxruntime-node to ~1.20.0, matching dustynv's 1.20.x
# libonnxruntime.so ABI.
RUN if [ "${WENDY_PLATFORM}" = "nvidia-jetson" ]; then \
        BUNDLED=$(find /app/node_modules/onnxruntime-node/bin -path '*linux/*' -name 'libonnxruntime.so*' | head -1) \
     && [ -n "$BUNDLED" ] || { echo "bundled libonnxruntime.so not found in onnxruntime-node" >&2; exit 1; } \
     && SYS_LIB=$(find / -name 'libonnxruntime.so*' 2>/dev/null | grep -v node_modules | grep -v __pycache__ | sort -r | head -1) \
     && [ -n "$SYS_LIB" ] || { echo "system libonnxruntime.so not found in dustynv image" >&2; exit 1; } \
     && ln -sf "$SYS_LIB" "$BUNDLED" \
     && echo "[onnxruntime-node] linked $BUNDLED -> $SYS_LIB"; \
    fi

COPY tsconfig.json ./
COPY src ./src
RUN npm run build

COPY --from=model-export /export/yolov8n.onnx ./yolov8n.onnx
COPY index.html ./
COPY assets/ ./assets/

EXPOSE {{.PORT}}

CMD ["node", "dist/index.js"]
