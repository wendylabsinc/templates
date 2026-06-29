# syntax=docker/dockerfile:1.6
#
# Base image is selected at `wendy run` time via the WENDY_PLATFORM build arg:
#   - nvidia-jetson -> debian-slim + TensorRT bindings; runs the custom fire/smoke
#                      YOLO on the CUDA 13.2 / cuDNN 9.20 / TensorRT 10.16 stack
#                      WendyOS injects via the `gpu` entitlement (Path-B; JETSON_JP7_GPU.md).
#   - generic       -> python:3.11-slim (CPU inference via Ultralytics).
# Falls back to "generic" when no build arg is supplied.
#
# The old dustynv/pytorch:*-cu128 (JetPack 6) base is gone on purpose — on a JP7.2
# Orin its CUDA 12.8 can't init against the host's 13.2 driver (Error 801) and falls
# back to CPU. WDY-1752.

ARG WENDY_PLATFORM=generic

# ── Model exporter: export fire.onnx from the committed fire.pt (CPU, Ultralytics) ──
FROM python:3.11-slim-bookworm AS model-exporter
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libxcb1 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir ultralytics
WORKDIR /export
COPY fire.pt ./fire.pt
# Static 320x320 input; the TRT engine is built for this size (YOLO_IMGSZ). 320 keeps
# the on-device engine build cheap (memory+time) so it finishes + caches before any restart.
RUN yolo export model=fire.pt format=onnx imgsz=320 && \
    [ -s /export/fire.onnx ] && \
    python3 -c "from ultralytics import YOLO; print('fire classes:', list(YOLO('fire.pt').names.values()))"

# ── NVIDIA Jetson base (Path-B: injected CUDA 13.2 + TensorRT 10.16, no torch) ──
FROM debian:bookworm-slim AS base-nvidia-jetson
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip \
    python3-gi \
    gir1.2-gst-plugins-base-1.0 \
    gir1.2-gst-plugins-bad-1.0 \
    gir1.2-gstreamer-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    v4l-utils \
    libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
# Full TensorRT 10.16.1 (bindings + libs) — the libs include libnvinfer_builder_resource,
# needed to BUILD the engine on-device (the injected stack is runtime-only). Pin all to
# 10.16.1 and let the wheel RPATH load them (the injected runtime is 10.16.2, a skew that
# breaks the builder); rely on the injected CUDA 13.2 + driver only.
RUN pip install --no-cache-dir --break-system-packages \
        fastapi "uvicorn[standard]" opencv-python-headless numpy && \
    pip install --no-cache-dir --break-system-packages --extra-index-url https://pypi.nvidia.com --no-deps \
        tensorrt-cu13==10.16.1.11 tensorrt-cu13-bindings==10.16.1.11 tensorrt-cu13-libs==10.16.1.11 && \
    pip install --no-cache-dir --break-system-packages cuda-python==13.2.0
ENV YOLO_BACKEND=trt \
    YOLO_IMGSZ=320 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=all \
    LD_LIBRARY_PATH=/usr/local/cuda-13.2/lib:/usr/local/cuda-13.2/lib64

# ── Generic CPU base (Raspberry Pi, x86, or any non-accelerated device) ──────
FROM python:3.11-slim-bookworm AS base-generic
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-gi \
    gir1.2-gst-plugins-base-1.0 \
    gir1.2-gst-plugins-bad-1.0 \
    gir1.2-gstreamer-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-nice \
    v4l-utils \
    libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ── Final stage: WENDY_PLATFORM selects the hardware path ────────────────────
FROM base-${WENDY_PLATFORM} AS final

WORKDIR /app

ARG WENDY_PLATFORM=generic
ARG WENDY_DEVICE_TYPE=""
ARG WENDY_HAS_GPU=""
ARG WENDY_GPU_VENDOR=""
ARG WENDY_JETPACK_VERSION=""
ARG WENDY_CUDA_VERSION=""
ARG WENDY_DEBUG=false
ENV WENDY_PLATFORM=${WENDY_PLATFORM} \
    WENDY_DEVICE_TYPE=${WENDY_DEVICE_TYPE} \
    WENDY_HAS_GPU=${WENDY_HAS_GPU} \
    WENDY_GPU_VENDOR=${WENDY_GPU_VENDOR} \
    WENDY_JETPACK_VERSION=${WENDY_JETPACK_VERSION} \
    WENDY_CUDA_VERSION=${WENDY_CUDA_VERSION} \
    WENDY_DEBUG=${WENDY_DEBUG}

# python3-gi installs bindings under /usr/lib/python3/dist-packages — expose them so
# `import gi` works on both bases, and sanity-check at build time.
ENV PYTHONPATH=/usr/lib/python3/dist-packages
RUN python3 -c "import gi; gi.require_version('Gst', '1.0'); from gi.repository import Gst; Gst.init(None); print('gi+Gst OK')"

# fire.onnx (from the exporter) drives the TensorRT GPU path; fire.pt is the CPU path's
# weights (Ultralytics). Both baked so no network is needed at runtime.
COPY --from=model-exporter /export/fire.onnx ./fire.onnx
COPY fire.pt ./fire.pt

RUN if [ "$WENDY_DEBUG" = "true" ]; then pip install --no-cache-dir --break-system-packages debugpy || pip install --no-cache-dir debugpy; fi

COPY app.py trt_yolo.py index.html ./
COPY assets/ ./assets/

ENV PYTHONUNBUFFERED=1

EXPOSE {{.PORT}}

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "{{.PORT}}"]
