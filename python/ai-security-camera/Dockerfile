# AI Security Camera — DeepStream YOLO detector for NVIDIA Jetson (WendyOS)
#
# Stage 1 (builder): compile the custom YOLO parser library and export the
#                    YOLO11n model to ONNX using the DeepStream 7.1 L4T image.
# Stage 2 (runtime): minimal Ubuntu 24.04 + Python venv + pyds. The DeepStream
#                    and CUDA runtime libraries are mounted from the host via CDI.

# ============================================================================
# Stage 1: Builder — compile custom YOLO library + export ONNX (CUDA 12.6)
# ============================================================================
FROM nvcr.io/nvidia/deepstream-l4t:7.1-triton-multiarch AS builder

RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    cmake \
    && rm -rf /var/lib/apt/lists/*

# Compile the custom YOLO inference/parser library against DeepStream 7.1.
RUN git clone https://github.com/marcoslucianops/DeepStream-Yolo.git /tmp/DeepStream-Yolo \
    && cd /tmp/DeepStream-Yolo \
    && export CUDA_VER=12.6 \
    && make -C nvdsinfer_custom_impl_Yolo \
    && mkdir -p /compiled \
    && cp nvdsinfer_custom_impl_Yolo/libnvdsinfer_custom_impl_Yolo.so /compiled/

# Export YOLO11n to ONNX in DeepStream-Yolo format (static batch=2, 640px).
# Pin torch to 2.3.x for compatibility with the export script.
RUN pip3 install --no-cache-dir 'torch<2.4' torchvision ultralytics onnx \
    && cd /tmp/DeepStream-Yolo \
    && python3 -c "from ultralytics import YOLO; YOLO('yolo11n.pt')" \
    && python3 utils/export_yolo11.py -w yolo11n.pt --batch 2 -s 640 \
    && cp yolo11n.onnx /compiled/ \
    && cp labels.txt /compiled/

# ============================================================================
# Stage 2: Runtime — Ubuntu 24.04 + Python 3.10 venv + pyds
# ============================================================================
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Python 3.10 (via deadsnakes) + GStreamer/GObject build & runtime deps.
# DeepStream libs, CUDA libs and DeepStream GStreamer plugins are mounted from
# the host via CDI at container start, so they are NOT installed here.
RUN apt-get update && apt-get install -y \
    build-essential \
    gnupg \
    ca-certificates \
    && echo "deb https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu noble main" > /etc/apt/sources.list.d/deadsnakes.list \
    && apt-key adv --keyserver keyserver.ubuntu.com --recv-keys F23C5A6CF475977595C89F51BA6932366A755776 \
    && apt-get update && apt-get install -y \
    python3.10 \
    python3.10-dev \
    python3.10-venv \
    libgirepository1.0-dev \
    libcairo2-dev \
    pkg-config \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-libav \
    libjsoncpp25 \
    && rm -rf /var/lib/apt/lists/*

# Isolated Python venv (avoids system package version mismatches).
RUN python3.10 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# PyGObject 3.46.0 is the last release that works cleanly with girepository-1.0.
RUN pip install --no-cache-dir \
    pycairo>=1.20.0 \
    PyGObject==3.46.0

# Application Python dependencies.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# DeepStream Python bindings (pyds) for aarch64 / Python 3.10.
RUN pip install --no-cache-dir \
    https://github.com/NVIDIA-AI-IOT/deepstream_python_apps/releases/download/v1.2.0/pyds-1.2.0-cp310-cp310-linux_aarch64.whl

WORKDIR /app
RUN mkdir -p /app/lib

# Custom YOLO library + exported ONNX model from the builder stage.
COPY --from=builder /compiled/libnvdsinfer_custom_impl_Yolo.so /app/lib/
COPY --from=builder /compiled/yolo11n.onnx /app/yolo11n.onnx

# Application files.
COPY security_camera.py /app/
COPY discovery.py /app/
COPY nvinfer_config.txt /app/
COPY tracker_config.yml /app/
COPY labels.txt /app/
COPY cameras.json /app/
COPY entrypoint.sh /app/

RUN chmod +x /app/security_camera.py /app/entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV GST_DEBUG=1
ENV EGL_PLATFORM=device
# DeepStream/CUDA library + plugin paths (populated from the host via CDI).
ENV LD_LIBRARY_PATH=/opt/nvidia/deepstream/deepstream-7.1/lib:/usr/lib/gstreamer-1.0/deepstream:/usr/local/cuda-12.6/lib:/usr/lib/aarch64-linux-gnu:/usr/lib
ENV GST_PLUGIN_PATH=/usr/lib/gstreamer-1.0/deepstream:/usr/lib/aarch64-linux-gnu/gstreamer-1.0/deepstream

# Web UI + MJPEG preview + metrics + events API.
EXPOSE {{.PORT}}

CMD ["/app/entrypoint.sh"]
