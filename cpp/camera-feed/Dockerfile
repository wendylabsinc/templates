# --------------------------------------------------------------------------
# Stage 1 – build
# --------------------------------------------------------------------------
FROM arm64v8/debian:bookworm-slim AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    ca-certificates \
    pkg-config \
    # Drogon dependencies
    libjsoncpp-dev \
    libssl-dev \
    uuid-dev \
    zlib1g-dev \
    # GStreamer dev packages
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    && rm -rf /var/lib/apt/lists/*

# Build Drogon from source (v1.9.12)
RUN git clone --depth 1 --branch v1.9.12 https://github.com/drogonframework/drogon.git /tmp/drogon && \
    cd /tmp/drogon && \
    git submodule update --init && \
    mkdir build && cd build && \
    cmake .. -DCMAKE_BUILD_TYPE=Release \
             -DBUILD_EXAMPLES=OFF \
             -DBUILD_CTL=OFF \
             -DBUILD_ORM=OFF && \
    make -j"$(nproc)" && \
    make install && \
    rm -rf /tmp/drogon

# Build application
WORKDIR /app
COPY CMakeLists.txt main.cpp ./
RUN mkdir build && cd build && \
    cmake .. -DCMAKE_BUILD_TYPE=Release && \
    make -j"$(nproc)"

# --------------------------------------------------------------------------
# Stage 2 – runtime
# --------------------------------------------------------------------------
FROM arm64v8/debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libjsoncpp25 \
    libssl3 \
    libuuid1 \
    zlib1g \
    # GStreamer runtime
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    v4l-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/build/{{.APP_ID}} .
COPY index.html ./index.html
COPY assets/ ./assets/

EXPOSE {{.PORT}}

CMD ["./{{.APP_ID}}"]
