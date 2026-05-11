# Dockerfile for NVIDIA Jetson Orin Nano
# Base: ultralytics/ultralytics jetson-jetpack6 (has PyTorch, OpenCV, ultralytics pre-installed)

# ---------- Frontend build stage ----------
FROM node:22-slim AS frontend-build

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---------- Runtime stage ----------
FROM ultralytics/ultralytics:latest-jetson-jetpack6

ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive

# Runtime apt deps:
# - bluez/libbluetooth-dev: Bluetooth stack
# - v4l-utils: v4l2-ctl, used by cameras.py / detector.py for capture-capability
#   filtering and the startup --list-formats-ext caps log
# - python3-gi + gir1.2-*: PyGObject bindings for any direct GStreamer use
# - gstreamer1.0-plugins-{base,good,bad,ugly,libav}: actual GStreamer element
#   implementations. The ultralytics Jetson base ships OpenCV compiled with
#   GStreamer support, but the v4l2src/jpegdec/videoconvert/appsink elements
#   themselves come from these apt packages. Without them, every
#   cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER) silently fails to construct.
#   Mirrors camera-feed-yolo, which works on hardware where this template
#   has been failing.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bluez libbluetooth-dev v4l-utils \
    python3-gi \
    gir1.2-gst-plugins-base-1.0 \
    gir1.2-gst-plugins-bad-1.0 \
    gir1.2-gstreamer-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps on top of the ultralytics base.
# torch, opencv-python, ultralytics are already installed in the base image.
# Chat/LLM deps (llama-cpp-python, huggingface-hub) are NOT included —
# enable with CHAT_ENABLED=1 and install them separately if needed.
RUN pip install --no-cache-dir \
        "fastapi>=0.115.0" \
        "uvicorn[standard]>=0.30.0" \
        "loro>=1.10.3" \
        "zeroconf>=0.148.0" \
        "psutil>=5.9.0" \
        "python-multipart>=0.0.9" \
        "requests>=2.31.0"

# Copy backend
WORKDIR /app/backend
COPY backend/ ./

# Copy frontend build output
COPY --from=frontend-build /build/frontend/build /app/frontend/build

EXPOSE 5702

ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=all
ENV PYTHONUNBUFFERED=1

# Chat/Agent (disabled by default — no Gemma 4 on Jetson)
ENV CHAT_ENABLED=0
ENV MODELS_DIR=/models

# YOLO detection config
ENV YOLO_MODELS_DIR=/yolo-models
ENV YOLO_MODEL=yolov8n.pt
ENV YOLO_DEVICE=0
ENV YOLO_IMGSZ=640
ENV YOLO_HALF=1
ENV YOLO_CONF=0.5
ENV YOLO_EXPORT_TRT=1
ENV YOLO_ENGINE=yolov8n.engine
ENV DRAW_OVERLAY=1
ENV TARGET_FPS=15
ENV JPEG_QUALITY=80
ENV USE_GSTREAMER=1
ENV CAMERA_SOURCE=v4l2
ENV CAMERA_INDEX=0
ENV CAMERA_WIDTH=1280
ENV CAMERA_HEIGHT=720
ENV CAMERA_FPS=30
# These only act as *size hints* for the matrix's size-hinted pipeline (#2)
# and as cap.set() values for the OpenCV fallback. The matrix tries an
# unconstrained pipeline first, so cameras pick their native best mode
# (usually 1080p or 720p MJPEG on modern UVC). The hint exists for cameras
# that won't negotiate "any caps" — those drop to 720p, still sharp at
# typical viewport sizes. 640x480 was tried briefly but looked blurry when
# the browser stretched it to fill the page.
ENV CAMERA_GSTREAMER_PIPELINE=""
# Make GStreamer's pipeline-construction and negotiation errors visible.
# Level 2 = ERROR only; cv2.VideoCapture(..., CAP_GSTREAMER) otherwise swallows
# them silently, leaving us blind when a pipeline fails to open.
ENV GST_DEBUG=2

# VLM config (Qwen3-VL service)
ENV VLM_ENABLED=1
ENV VLM_URL=http://127.0.0.1:8090
ENV VLM_CONF_THRESHOLD=0.7
ENV VLM_TIMEOUT=120
ENV VLM_QUESTION=""

# Logging
ENV LOG_DIR=/logs
ENV DISABLE_ONNXRUNTIME=1

CMD ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5702", "--timeout-keep-alive", "5", "--no-access-log"]
