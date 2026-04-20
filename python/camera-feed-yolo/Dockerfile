# WENDY_PLATFORM selects the hardware acceleration tier; defaults to generic (CPU).
# Set automatically by the CLI based on the connected device type.
ARG WENDY_PLATFORM=generic

# ── NVIDIA Jetson base (CUDA-accelerated via L4T) ────────────────────────────
FROM dustynv/pytorch:2.7-r36.4.0-cu128-24.04 AS base-nvidia-jetson

# L4T base already includes CUDA-enabled torch, torchvision, OpenCV, numpy.
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

WORKDIR /app

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

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Final stage: WENDY_PLATFORM selects the hardware path ────────────────────
FROM base-${WENDY_PLATFORM}

ARG WENDY_DEVICE_TYPE
ARG WENDY_DEBUG=false
ENV WENDY_DEVICE_TYPE=${WENDY_DEVICE_TYPE}
ENV WENDY_DEBUG=${WENDY_DEBUG}

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

RUN if [ "$WENDY_DEBUG" = "true" ]; then pip install --no-cache-dir debugpy; fi

COPY app.py index.html ./
COPY assets/ ./assets/

ENV PYTHONUNBUFFERED=1

EXPOSE {{.PORT}}

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "{{.PORT}}"]
