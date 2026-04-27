# syntax=docker/dockerfile:1.6
#
# Multi-stage build keyed on WENDY_PLATFORM.
#   nvidia-jetson -> dustynv/onnxruntime + Swift toolchain (CUDA EP)
#   generic       -> swift:{{.SWIFT_VERSION}}-bookworm + Microsoft prebuilt CPU onnxruntime

ARG WENDY_PLATFORM=generic

# ── Stage A: export yolov8n.onnx ─────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS model-export
RUN pip install --no-cache-dir ultralytics onnx
WORKDIR /export
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt').export(format='onnx', imgsz=640, opset=12, simplify=True, dynamic=False)" \
 && [ -s /export/yolov8n.onnx ] \
 && [ "$(stat -c %s /export/yolov8n.onnx)" -gt 5000000 ]

# ── Stage B-jetson: Swift on top of CUDA onnxruntime ────────────────────────
FROM dustynv/onnxruntime:1.20.0-r36.4.0 AS base-nvidia-jetson

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates pkg-config gnupg \
    binutils libc6-dev libcurl4 libedit2 libgcc-12-dev libpython3-dev \
    libsqlite3-0 libstdc++-12-dev libxml2 libz3-dev \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    v4l-utils libturbojpeg0-dev \
    && rm -rf /var/lib/apt/lists/*

# Swift toolchain (aarch64). Mirrors the official swift.org Linux release.
ARG SWIFT_VERSION_BUILD={{.SWIFT_VERSION}}
RUN curl -fsSL "https://download.swift.org/swift-${SWIFT_VERSION_BUILD}-release/ubuntu2204-aarch64/swift-${SWIFT_VERSION_BUILD}-RELEASE/swift-${SWIFT_VERSION_BUILD}-RELEASE-ubuntu22.04-aarch64.tar.gz" \
      -o /tmp/swift.tgz \
 && tar -xzf /tmp/swift.tgz -C /usr/local --strip-components=1 \
 && rm /tmp/swift.tgz

# Surface libonnxruntime.so + headers under /usr/local + write a .pc file so
# Swift's pkgConfig systemLibrary target finds them.
RUN LIB=$(find / -name "libonnxruntime.so*" 2>/dev/null | grep -v __pycache__ | sort -r | head -1) \
 && [ -n "$LIB" ] || (echo "libonnxruntime.so not found" >&2 && exit 1) \
 && ln -sf "$LIB" /usr/local/lib/libonnxruntime.so \
 && HDR=$(find / -name "onnxruntime_c_api.h" 2>/dev/null | head -1) \
 && [ -n "$HDR" ] || (echo "onnxruntime_c_api.h not found" >&2 && exit 1) \
 && cp "$(dirname "$HDR")"/*.h /usr/local/include/ \
 && mkdir -p /usr/local/lib/pkgconfig \
 && printf 'prefix=/usr/local\nexec_prefix=${prefix}\nlibdir=${exec_prefix}/lib\nincludedir=${prefix}/include\n\nName: libonnxruntime\nDescription: ONNX Runtime\nVersion: 1.20.0\nLibs: -L${libdir} -lonnxruntime\nCflags: -I${includedir}\n' > /usr/local/lib/pkgconfig/libonnxruntime.pc \
 && ldconfig

# ── Stage B-generic: Swift base + CPU onnxruntime ───────────────────────────
FROM swift:{{.SWIFT_VERSION}}-bookworm AS base-generic

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
 && mkdir -p /usr/local/lib/pkgconfig \
 && printf 'prefix=/usr/local\nexec_prefix=${prefix}\nlibdir=${exec_prefix}/lib\nincludedir=${prefix}/include\n\nName: libonnxruntime\nDescription: ONNX Runtime\nVersion: 1.20.0\nLibs: -L${libdir} -lonnxruntime\nCflags: -I${includedir}\n' > /usr/local/lib/pkgconfig/libonnxruntime.pc \
 && ldconfig

# ── Stage C: build the Swift app on the chosen base ─────────────────────────
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
COPY Package.swift ./
COPY Sources Sources
RUN swift build -c release \
 && cp ".build/release/{{.APP_ID}}" /usr/local/bin/{{.APP_ID}} \
 && rm -rf .build

COPY --from=model-export /export/yolov8n.onnx ./yolov8n.onnx
COPY index.html ./
COPY assets/ ./assets/

EXPOSE {{.PORT}}

CMD ["{{.APP_ID}}"]
