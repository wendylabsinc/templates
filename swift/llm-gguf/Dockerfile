# Build frontend
FROM node:22-slim AS frontend-builder

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Build Swift server and llama-cli
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

COPY llama.cpp.tar.gz /tmp/llama.cpp.tar.gz

ARG CUDA_ARCH=87

RUN mkdir -p /opt/llama.cpp \
    && tar -xzf /tmp/llama.cpp.tar.gz -C /opt/llama.cpp --strip-components=1 \
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
COPY --from=builder /repo/.build/debug/qwen3-chat-server /usr/local/bin/qwen3-chat-server
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

CMD ["qwen3-chat-server"]
