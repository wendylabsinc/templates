# syntax=docker/dockerfile:1.6
#
# Multi-stage build keyed on WENDY_PLATFORM.
#   nvidia-jetson -> dustynv/onnxruntime + Node 22 (CUDA EP via system libonnxruntime;
#                    onnxruntime-node will use its bundled CPU EP on aarch64,
#                    same fallback the Python path takes when CUDA isn't usable).
#   generic       -> node:22-slim + Microsoft prebuilt CPU onnxruntime.

ARG WENDY_PLATFORM=generic

# ── Stage A: export yolov8n.onnx ─────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS model-export
RUN pip install --no-cache-dir ultralytics onnx
WORKDIR /export
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt').export(format='onnx', imgsz=640, opset=12, simplify=True, dynamic=False)" \
 && [ -s /export/yolov8n.onnx ] \
 && [ "$(stat -c %s /export/yolov8n.onnx)" -gt 5000000 ]

# ── Stage B-jetson ──────────────────────────────────────────────────────────
FROM dustynv/onnxruntime:1.20.0-r36.4.0 AS base-nvidia-jetson

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
RUN npm install
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

COPY --from=model-export /export/yolov8n.onnx ./yolov8n.onnx
COPY index.html ./
COPY assets/ ./assets/

EXPOSE {{.PORT}}

CMD ["node", "dist/index.js"]
