# syntax=docker/dockerfile:1.6
FROM arm64v8/debian:bookworm-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    ca-certificates \
    ccache \
    libjsoncpp-dev \
    libssl-dev \
    uuid-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

ENV CCACHE_DIR=/ccache

WORKDIR /deps

RUN --mount=type=cache,target=/ccache \
    git clone --branch v1.9.12 --depth 1 https://github.com/drogonframework/drogon.git && \
    cd drogon && \
    git submodule update --init && \
    cmake -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache -DBUILD_EXAMPLES=OFF -DBUILD_CTL=OFF -DBUILD_ORM=OFF && \
    cmake --build build -j$(nproc) && \
    cmake --install build

WORKDIR /app

COPY CMakeLists.txt main.cpp ./

RUN --mount=type=cache,target=/ccache \
    cmake -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache && \
    cmake --build build --config Release

FROM arm64v8/debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libjsoncpp25 \
    libssl3 \
    libuuid1 \
    zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/build/{{.APP_ID}} /usr/local/bin/{{.APP_ID}}

EXPOSE {{.PORT}}

CMD ["{{.APP_ID}}"]
