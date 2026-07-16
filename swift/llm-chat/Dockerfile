# syntax=docker/dockerfile:1.6
# Build frontend (shared by both backends)
FROM node:22-slim AS frontend-builder

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

{{ if eq .BACKEND "gguf" }}
# Build Swift server and llama-cli (GGUF backend, Jetson/CUDA via llama.cpp)
FROM dustynv/tensorrt:8.6-r36.2.0 AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    ca-certificates \
    clang \
    cmake \
    curl \
    git \
    libcurl4-openssl-dev \
    libedit2 \
    libicu-dev \
    libsqlite3-0 \
    libxml2 \
    libncurses6 \
    libstdc++6 \
    libgcc-s1 \
    make \
    ninja-build \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/swift \
    && curl -fSL https://download.swift.org/swift-6.2.3-release/ubuntu2204-aarch64/swift-6.2.3-RELEASE/swift-6.2.3-RELEASE-ubuntu22.04-aarch64.tar.gz \
    | tar -xz -C /opt/swift --strip-components=1

ENV PATH="/opt/swift/usr/bin:${PATH}"

ARG CUDA_ARCH=87

# llama.cpp is fetched from a pinned upstream release tag rather than a
# vendored tarball, so the build is reproducible from a clean checkout.
ARG LLAMA_CPP_REF=b4000

RUN git clone --depth 1 --branch ${LLAMA_CPP_REF} https://github.com/ggerganov/llama.cpp /opt/llama.cpp \
    && cmake -S /opt/llama.cpp -B /opt/llama.cpp/build -G Ninja \
        -DGGML_CUDA=ON \
        -DGGML_CUDA_NO_VMM=ON \
        -DGGML_CURL=ON \
        -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH} \
        -DCMAKE_SHARED_LINKER_FLAGS="-L/usr/local/cuda/lib64/stubs -lcuda" \
        -DCMAKE_EXE_LINKER_FLAGS="-L/usr/local/cuda/lib64/stubs -lcuda" \
        -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /opt/llama.cpp/build --target llama-cli -j

WORKDIR /repo

COPY server/Package.swift server/Package.resolved* ./
COPY server/Sources ./Sources

RUN swift package resolve
RUN swift build

# Runtime stage
FROM dustynv/tensorrt:8.6-r36.2.0

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    ca-certificates \
    libcurl4 \
    libedit2 \
    libicu70 \
    libsqlite3-0 \
    libxml2 \
    libncurses6 \
    libstdc++6 \
    libgcc-s1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /opt/swift/usr/lib/swift /opt/swift/usr/lib/swift
COPY --from=builder /repo/.build/debug/llm-server-gguf /usr/local/bin/llm-server-gguf
COPY --from=builder /opt/llama.cpp/build/bin/llama-cli /opt/llama/llama-cli
COPY --from=frontend-builder /app/frontend/dist /app/frontend/dist

ENV FRONTEND_DIST=/app/frontend/dist
ENV PATH="/opt/llama:${PATH}"
ENV LD_LIBRARY_PATH="/opt/swift/usr/lib/swift/linux:/opt/swift/usr/lib/swift"

ENV QWEN_MODEL="Qwen/Qwen3-4B-GGUF:Q4_K_M"
ENV QWEN_CTX="2048"
ENV QWEN_TOKENS="256"
ENV QWEN_NGL="99"
ENV QWEN_TEMP="0.6"
ENV QWEN_TOP_P="0.95"
ENV QWEN_TOP_K="20"
ENV QWEN_MAX_TOKENS="1024"
ENV QWEN_SYSTEM="You are a concise assistant for Jetson Orin Nano. /no_think"

EXPOSE {{.PORT}}

CMD ["llm-server-gguf"]
{{ else }}
# Build Swift server (MLX backend, CUDA GPU acceleration)
#
# MLX-Swift is NOT Apple-only: mlx-swift gained a CUDA backend on Linux
# (github.com/ml-explore/mlx-swift, gated by the `SPM_CUDA` SwiftPM
# environment check, which defaults to CUDA whenever it isn't explicitly
# set to "0"). On Linux that CUDA path needs the CUDA toolkit + cuDNN
# present at build time, plus the header-only cuDNN Frontend and CUTLASS
# libraries on the include path -- mlx's C++ core `#include`s
# <cudnn_frontend.h> directly and does not vendor it. This mirrors the
# base image / library setup already proven in swift/cuda-llm's Dockerfile.
FROM --platform=linux/arm64 ubuntu:24.04 AS mlx-cuda-base

ENV DEBIAN_FRONTEND=noninteractive

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

# CUDA 13.2 + cuDNN 9 -- installed by versioned name to avoid conflicts with
# Ubuntu's own nvidia-cuda-dev / nvidia-cudnn packages (which pin older versions).
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

# cuDNN Frontend (header-only library; mlx's cuda backend #includes it directly)
RUN git clone --depth 1 https://github.com/NVIDIA/cudnn-frontend.git /opt/cudnn-frontend
ENV CUDNN_FRONTEND_PATH="/opt/cudnn-frontend"

# CUTLASS (header-only library)
RUN git clone --depth 1 https://github.com/NVIDIA/cutlass.git /opt/cutlass
ENV CUTLASS_PATH="/opt/cutlass"

FROM mlx-cuda-base AS swift-mlx-builder

# Swift toolchain (aarch64), a STABLE release -- mirrors llm-server-gguf's
# toolchain. MLX-Swift's CUDA codegen has hit an IRGenDebugInfo assertion on
# Swift nightly/dev snapshots, so keep this pinned to a swift.org release.
RUN mkdir -p /opt/swift \
    && curl -fSL https://download.swift.org/swift-6.2.3-release/ubuntu2404-aarch64/swift-6.2.3-RELEASE/swift-6.2.3-RELEASE-ubuntu24.04-aarch64.tar.gz \
    | tar -xz -C /opt/swift --strip-components=1

ENV PATH="/opt/swift/usr/bin:${PATH}"

ENV CPLUS_INCLUDE_PATH="/opt/cutlass/include:/opt/cutlass/tools/util/include:/opt/cudnn-frontend/include"
ENV CUDA_ARCH=sm_87

WORKDIR /repo

COPY server/Package.swift server/Package.resolved* ./
COPY server/Sources ./Sources

RUN swift package resolve
RUN --mount=type=cache,target=/root/.swiftpm swift build -c release --jobs=2 -Xcc -w \
    && cp .build/release/llm-server-mlx /usr/local/bin/llm-server-mlx \
    && rm -rf .build

COPY --from=frontend-builder /app/frontend/dist /app/frontend/dist

ENV FRONTEND_DIST=/app/frontend/dist
ENV MODEL_ID=mlx-community/TinyLlama-1.1B-Chat-v1.0-4bit

EXPOSE {{.PORT}}

CMD ["llm-server-mlx"]
{{ end }}
