# syntax=docker/dockerfile:1.6
#
# Base image is selected at `wendy run` time via the WENDY_PLATFORM build arg
# that the wendy CLI injects after querying the target device:
#   - nvidia-jetson -> dustynv/pytorch with CUDA/JetPack
#   - generic       -> python:3.11-slim-bookworm (CPU inference)
# Falls back to "generic" when no build arg is supplied (e.g. plain `docker build`).

ARG WENDY_PLATFORM=generic

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
RUN pip3 install --no-cache-dir \
    --index-url https://pypi.org/simple/ \
    fastapi \
    'uvicorn[standard]' \
    ultralytics && \
    ln -sf /opt/venv/bin/uvicorn /usr/local/bin/uvicorn && \
    ln -sf /opt/venv/bin/python3 /usr/local/bin/python3-venv


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

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt


# Final stage resolves to base-nvidia-jetson or base-generic based on WENDY_PLATFORM.
FROM base-${WENDY_PLATFORM} AS final

WORKDIR /app

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

COPY app.py index.html ./
COPY assets/ ./assets/

ENV PYTHONUNBUFFERED=1

# Surface the device capabilities the CLI passed in so app.py can choose
# GPU vs CPU inference at runtime without re-detecting hardware.
ARG WENDY_PLATFORM=generic
ARG WENDY_DEVICE_TYPE=""
ARG WENDY_HAS_GPU=""
ARG WENDY_GPU_VENDOR=""
ARG WENDY_JETPACK_VERSION=""
ARG WENDY_CUDA_VERSION=""
ENV WENDY_PLATFORM=${WENDY_PLATFORM} \
    WENDY_DEVICE_TYPE=${WENDY_DEVICE_TYPE} \
    WENDY_HAS_GPU=${WENDY_HAS_GPU} \
    WENDY_GPU_VENDOR=${WENDY_GPU_VENDOR} \
    WENDY_JETPACK_VERSION=${WENDY_JETPACK_VERSION} \
    WENDY_CUDA_VERSION=${WENDY_CUDA_VERSION}

EXPOSE {{.PORT}}

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "{{.PORT}}"]
