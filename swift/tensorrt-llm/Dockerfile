# Build stage
FROM dustynv/tensorrt:8.6-r36.2.0 AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    ca-certificates \
    clang \
    curl \
    libcurl4-openssl-dev \
    libedit2 \
    libicu-dev \
    libsqlite3-0 \
    libxml2 \
    libncurses6 \
    libstdc++6 \
    libgcc-s1 \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/swift \
    && curl -fSL https://download.swift.org/swift-6.2.3-release/ubuntu2204-aarch64/swift-6.2.3-RELEASE/swift-6.2.3-RELEASE-ubuntu22.04-aarch64.tar.gz \
    | tar -xz -C /opt/swift --strip-components=1

ENV PATH="/opt/swift/usr/bin:${PATH}"
ENV LIBRARY_PATH="/usr/local/cuda/targets/aarch64-linux/lib/stubs"

RUN cp /usr/local/cuda/targets/aarch64-linux/lib/stubs/libcuda.so /lib/aarch64-linux-gnu/nvidia/libcuda.so

WORKDIR /repo

COPY . .

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
COPY --from=builder /repo/.build/debug/tensorrt-llm-streaming /app/tensorrt-llm-streaming

ENV LD_LIBRARY_PATH="/opt/swift/usr/lib/swift/linux:/opt/swift/usr/lib/swift"

CMD ["/app/tensorrt-llm-streaming"]
