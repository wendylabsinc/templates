# syntax=docker/dockerfile:1.6
# Stage 1: build the Swift node. Eclipse CycloneDDS is built from source because
# swift-ros2 resolves CycloneDDS through pkg-config on Linux (systemLibrary
# pkgConfig: "CycloneDDS"), and the ROS apt packages target Ubuntu, not Debian.
FROM swift:6.3-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
      cmake git build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch 0.10.5 \
      https://github.com/eclipse-cyclonedds/cyclonedds.git /tmp/cyclonedds \
 && cmake -S /tmp/cyclonedds -B /tmp/cyclonedds/build \
      -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_IDLC=OFF -DENABLE_SHM=OFF -DBUILD_TESTING=OFF -DBUILD_EXAMPLES=OFF \
 && cmake --build /tmp/cyclonedds/build --target install -j"$(nproc)" \
 && rm -rf /tmp/cyclonedds

ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig
ENV LD_LIBRARY_PATH=/usr/local/lib

WORKDIR /app
COPY Package.swift ./
COPY Sources Sources
RUN --mount=type=cache,id=swiftpm-{{.APP_ID}}-listener,target=/app/.build \
    swift build -c release \
 && cp .build/release/listener /app/listener

# Stage 2: slim runtime with just the CycloneDDS shared library + the binary.
FROM swift:6.3-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /usr/local/lib/libddsc.so* /usr/local/lib/
RUN ldconfig
COPY --from=builder /app/listener /usr/local/bin/listener
CMD ["listener"]
