# Phase 1 base. Cross-platform (Mac dev / linux arm64 on the Jetson).
# Phase 2 will switch to nvcr.io/nvidia/l4t-jetpack:r35.3.1 +
# unitree_sdk2_python so the real-dog SportClient path works.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build deps for cyclonedds C library + Python bindings. cyclonedds-python
# only ships prebuilt wheels for macOS arm64 + Linux x86_64; on linux/arm64
# (the Jetson target) pip falls back to building from sdist, which needs
# the cyclonedds C library installed first.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        cmake \
        build-essential \
        git \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Build CycloneDDS C library from source. Pin to the version unitree_sdk2_python
# expects (0.10.5) so this image stays compatible when we add real-dog
# SportClient support in Phase 2.
ARG CYCLONEDDS_VERSION=0.10.5
RUN git clone --depth 1 --branch ${CYCLONEDDS_VERSION} \
        https://github.com/eclipse-cyclonedds/cyclonedds.git /tmp/cyclonedds \
    && cmake -S /tmp/cyclonedds -B /tmp/cyclonedds/build \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DBUILD_EXAMPLES=OFF \
        -DBUILD_TESTING=OFF \
        -DBUILD_IDLC=ON \
    && cmake --build /tmp/cyclonedds/build -j"$(nproc)" \
    && cmake --install /tmp/cyclonedds/build \
    && ldconfig \
    && rm -rf /tmp/cyclonedds

ENV CYCLONEDDS_HOME=/usr/local

WORKDIR /app

COPY pyproject.toml ./
# Pin cyclonedds-python to the 0.10.x branch — it links against private
# C-lib headers (ddsi_radmin.h etc.) and only the matching python wheel
# version compiles against the C lib version we built (0.10.5). The
# 11.0.x line of the python package targets cyclonedds 0.11+ headers.
RUN pip install --no-cache-dir 'cyclonedds<0.11' rich httpx

# Brain doesn't link unitree_sdk2_python directly. Real-dog motion lives
# in a sibling `go2-motion` container that wraps SportClient behind an
# HTTP API; brain calls that over localhost on the Jetson.

COPY src/ ./src/
ENV PYTHONPATH=/app/src

# Safety: require fresh /go2/perception/free_space data before driving the
# dog. With strict=1, missing or stale LIDAR free-space → vx clamped to 0
# (yaw still allowed). Set to 0 (or unset) for permissive mode where
# missing free-space is treated as "clear" — fine for sim and dev,
# unsafe on real hardware once the LIDAR pipeline is reliable.
ENV BRAIN_SAFETY_STRICT=1

ENTRYPOINT ["python", "-m", "brain.main"]
