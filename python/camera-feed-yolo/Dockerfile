# syntax=docker/dockerfile:1.6
#
# Base image is selected at `wendy run` time via the WENDY_PLATFORM build arg
# that the wendy CLI injects after querying the target device:
#   - nvidia-jetson -> dustynv/pytorch with CUDA/JetPack
#   - generic       -> python:3.11-slim-bookworm (CPU inference)
# Falls back to "generic" when no build arg is supplied (e.g. plain `docker build`).

ARG WENDY_PLATFORM=generic

# ── NVIDIA Jetson base (CUDA-accelerated via L4T) ────────────────────────────
FROM dustynv/pytorch:2.7-r36.4.0-cu128-24.04 AS base-nvidia-jetson

# L4T base already ships CUDA torch/torchvision/OpenCV/numpy.
# Add GStreamer + v4l so USB cameras work; install app-level Python deps on top.
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
    v4l-utils \
    && rm -rf /var/lib/apt/lists/*

# dustynv installs pip packages into /opt/venv — put its bin dir on PATH so
# `uvicorn` and `python3` both resolve to the venv (OCI exec otherwise uses
# /usr/local/bin/python3 which can't see the venv's site-packages).
ENV PATH="/opt/venv/bin:${PATH}"

# Pip will see the pre-installed torch>=2.2 and skip reinstalling it for ultralytics.
# The dustynv base pins pip to Jetson-specific indexes that don't mirror fastapi/uvicorn —
# override with real PyPI. Torch is pre-installed so ultralytics won't pull a CPU wheel.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip3 install \
    --index-url https://pypi.org/simple/ \
    fastapi \
    'uvicorn[standard]' \
    ultralytics && \
    ln -sf /opt/venv/bin/uvicorn /usr/local/bin/uvicorn && \
    ln -sf /opt/venv/bin/python3 /usr/local/bin/python3-venv

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
# Build-time guard: no GPU exists at build time so torch.cuda.is_available()
# can't be asserted here; at least confirm the CUDA-12 runtime libs landed.
RUN test -n "$(ls /opt/cuda12/lib/libcudart.so* 2>/dev/null)"


# ── Generic CPU base (Raspberry Pi, unknown, or any non-accelerated device) ──
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
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install CPU-only torch first so ultralytics' transitive torch dependency is
# already satisfied: on aarch64 the default PyPI wheel is CUDA-enabled and adds
# ~9 GB of dead weight to a stage that has no GPU (9.68 GB → 0.68 GB measured).
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url https://download.pytorch.org/whl/cpu \
    torch torchvision

COPY requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /tmp/requirements.txt


# ── Final stage: WENDY_PLATFORM selects the hardware path ────────────────────
FROM base-${WENDY_PLATFORM} AS final

WORKDIR /app

# Surface the device capabilities the CLI passed in so app.py can choose
# GPU vs CPU inference at runtime without re-detecting hardware.
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

# Both bases install python3-gi via apt, which drops bindings under
# /usr/lib/python3/dist-packages — a path the python image's /usr/local Python
# and dustynv's /opt/venv Python don't include by default. Expose it via
# PYTHONPATH so `import gi` works, and sanity-check at build time so a
# regression fails loudly instead of silently disabling GStreamer.
ENV PYTHONPATH=/usr/lib/python3/dist-packages
RUN python3 -c "import gi; gi.require_version('Gst', '1.0'); from gi.repository import Gst; Gst.init(None); print('gi+Gst OK')"

# Bake YOLOv8n weights into the image so the container doesn't need network on first run.
# Size check catches truncated downloads (full file is ~6.2 MB).
RUN python3 -c "from ultralytics import YOLO; YOLO('yolov8n.pt')" && \
    [ -s /app/yolov8n.pt ] && [ "$(stat -c %s /app/yolov8n.pt)" -gt 6000000 ]

RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$WENDY_DEBUG" = "true" ]; then pip install debugpy; fi

COPY app.py index.html ./
COPY assets/ ./assets/

ENV PYTHONUNBUFFERED=1

EXPOSE {{.PORT}}

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "{{.PORT}}"]
