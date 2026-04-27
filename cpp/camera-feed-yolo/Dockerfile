# syntax=docker/dockerfile:1.6
#
# Multi-stage build keyed on WENDY_PLATFORM.
#   nvidia-jetson -> dustynv/onnxruntime (CUDA-built libonnxruntime.so)
#   generic       -> debian:bookworm-slim + Microsoft prebuilt CPU onnxruntime

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
    build-essential cmake git pkg-config ca-certificates \
    libjsoncpp-dev libssl-dev uuid-dev zlib1g-dev \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    v4l-utils libturbojpeg0-dev \
    && rm -rf /var/lib/apt/lists/*

# dustynv ships onnxruntime as a Python wheel; expose libonnxruntime.so + headers
# under /usr/local so find_library / find_path succeed.
RUN LIB=$(find / -name "libonnxruntime.so*" 2>/dev/null | grep -v __pycache__ | sort -r | head -1) \
 && [ -n "$LIB" ] || (echo "libonnxruntime.so not found" >&2 && exit 1) \
 && ln -sf "$LIB" /usr/local/lib/libonnxruntime.so \
 && HDR=$(find / -name "onnxruntime_cxx_api.h" 2>/dev/null | head -1) \
 && [ -n "$HDR" ] || (echo "onnxruntime_cxx_api.h not found" >&2 && exit 1) \
 && mkdir -p /usr/local/include/onnxruntime \
 && cp "$(dirname "$HDR")"/*.h /usr/local/include/onnxruntime/ \
 && ldconfig

# Build Drogon from source.
RUN git clone --depth 1 --branch v1.9.12 https://github.com/drogonframework/drogon.git /tmp/drogon \
 && cd /tmp/drogon && git submodule update --init \
 && mkdir build && cd build \
 && cmake .. -DCMAKE_BUILD_TYPE=Release -DBUILD_EXAMPLES=OFF -DBUILD_CTL=OFF -DBUILD_ORM=OFF \
 && make -j"$(nproc)" && make install \
 && rm -rf /tmp/drogon

# ── Stage B-generic ─────────────────────────────────────────────────────────
FROM debian:bookworm-slim AS base-generic

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git pkg-config ca-certificates curl \
    libjsoncpp-dev libssl-dev uuid-dev zlib1g-dev \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    v4l-utils libturbojpeg0-dev \
    && rm -rf /var/lib/apt/lists/*

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

RUN git clone --depth 1 --branch v1.9.12 https://github.com/drogonframework/drogon.git /tmp/drogon \
 && cd /tmp/drogon && git submodule update --init \
 && mkdir build && cd build \
 && cmake .. -DCMAKE_BUILD_TYPE=Release -DBUILD_EXAMPLES=OFF -DBUILD_CTL=OFF -DBUILD_ORM=OFF \
 && make -j"$(nproc)" && make install \
 && rm -rf /tmp/drogon

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
COPY CMakeLists.txt main.cpp ./
RUN mkdir build && cd build \
 && cmake .. -DCMAKE_BUILD_TYPE=Release \
 && make -j"$(nproc)" \
 && cp {{.APP_ID}} /app/{{.APP_ID}} \
 && cd /app && rm -rf build

COPY --from=model-export /export/yolov8n.onnx ./yolov8n.onnx
COPY index.html ./
COPY assets/ ./assets/

EXPOSE {{.PORT}}

CMD ["./{{.APP_ID}}"]
