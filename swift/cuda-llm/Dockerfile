# syntax=docker/dockerfile:1.6

ARG WENDY_PLATFORM=generic

FROM --platform=linux/arm64 ubuntu:24.04 as cuda-base

# Base build tools
RUN apt-get update && apt-get install -y \
    wget \
    curl \
    gnupg2 \
    ca-certificates \
    build-essential \
    git \
    cmake \
    python3 \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# NVIDIA L4T repos for JetPack 7.2 / L4T r39.2
RUN wget -q https://repo.download.nvidia.com/jetson/jetson-ota-public.asc -O /etc/apt/trusted.gpg.d/jetson-ota-public.asc \
    && echo "deb https://repo.download.nvidia.com/jetson/common r39.2 main" > /etc/apt/sources.list.d/nvidia-l4t-apt-source.list \
    && echo "deb https://repo.download.nvidia.com/jetson/som r39.2 main" >> /etc/apt/sources.list.d/nvidia-l4t-apt-source.list \
    && echo "deb https://repo.download.nvidia.com/jetson/ffmpeg r39.2 main" >> /etc/apt/sources.list.d/nvidia-l4t-apt-source.list \
    && apt-get update

# CUDA 13.2 + cuDNN 9 — install by versioned name to avoid conflicts with Ubuntu's own
# nvidia-cuda-dev / nvidia-cudnn packages (which pin to older versions).
RUN apt-get install -y \
    cuda-toolkit-13-2 \
    cuda-driver-dev-13-2 \
    libcudnn9-cuda-13 \
    libcudnn9-dev-cuda-13 \
    libcudnn9-headers-cuda-13 \
    && rm -rf /var/lib/apt/lists/*

# Additional packages + Swift runtime dependencies
RUN apt-get update && apt-get install -y \
    libblas-dev liblapack-dev liblapacke-dev \
    libncurses6 libncursesw6 \
    libicu-dev \
    libxml2-dev \
    libz3-dev \
    libedit-dev \
    gfortran \
    libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/usr/local/cuda/bin:${PATH}" \
    LD_LIBRARY_PATH="/usr/local/cuda/lib64:${LD_LIBRARY_PATH}"

# cuDNN frontend (header-only library)
RUN git clone --depth 1 https://github.com/NVIDIA/cudnn-frontend.git /opt/cudnn-frontend
ENV CUDNN_FRONTEND_PATH="/opt/cudnn-frontend"

# CUTLASS (header-only library)
RUN git clone --depth 1 https://github.com/NVIDIA/cutlass.git /opt/cutlass
ENV CUTLASS_PATH="/opt/cutlass"

FROM cuda-base as swift-cuda

# Swift toolchain (aarch64). Mirrors the official swift.org Linux release.
ARG SWIFT_VERSION_BUILD=6.3.1
RUN curl -fsSL "https://download.swift.org/swift-${SWIFT_VERSION_BUILD}-release/ubuntu2404-aarch64/swift-${SWIFT_VERSION_BUILD}-RELEASE/swift-${SWIFT_VERSION_BUILD}-RELEASE-ubuntu24.04-aarch64.tar.gz" \
      -o /tmp/swift.tgz \
 && tar -xzf /tmp/swift.tgz -C / --strip-components=1 \
 && rm /tmp/swift.tgz

# Build the app
FROM swift-cuda as app

ENV CPLUS_INCLUDE_PATH="$CUTLASS_PATH/include:$CUTLASS_PATH/tools/util/include:$CUDNN_FRONTEND_PATH/include"
ENV CUDA_ARCH=sm_87

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
RUN --mount=type=cache,target=/root/.swiftpm swift build -c release --jobs=2 -Xcc -w \
 && cp ".build/release/cuda-llm" /usr/local/bin/cuda-llm \
 && rm -rf .build

CMD ["cuda-llm"]
