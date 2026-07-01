# syntax=docker/dockerfile:1.6
# go2-foxglove — bridge (Go2 DDS → Foxglove WebSocket).
#
# Same proven base as the go2-initial-test SDK services: Ubuntu 22.04 +
# CycloneDDS 0.10.5 from source + unitree_sdk2_python pinned, so the SDK bindings
# and the cyclonedds python wheel link on linux/arm64. Build for arm64 (the Go2's
# Orin): `--platform linux/arm64` if building from an x86 host.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-dev git build-essential cmake pkg-config ca-certificates ccache \
    && rm -rf /var/lib/apt/lists/*
ENV CCACHE_DIR=/ccache

ARG CYCLONEDDS_VERSION=0.10.5
RUN --mount=type=cache,target=/ccache \
    git clone --depth 1 --branch ${CYCLONEDDS_VERSION} \
        https://github.com/eclipse-cyclonedds/cyclonedds.git /tmp/cyclonedds \
    && cmake -S /tmp/cyclonedds -B /tmp/cyclonedds/build \
        -DCMAKE_INSTALL_PREFIX=/usr/local -DBUILD_EXAMPLES=OFF -DBUILD_TESTING=OFF -DBUILD_IDLC=ON \
        -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
    && cmake --build /tmp/cyclonedds/build -j"$(nproc)" \
    && cmake --install /tmp/cyclonedds/build && ldconfig && rm -rf /tmp/cyclonedds
ENV CYCLONEDDS_HOME=/usr/local

WORKDIR /app
ARG UNITREE_SDK2_PYTHON_REF=db9b2d210081387fcd1e7ed9ac4c56a02983bb85
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip3 install --upgrade pip \
    && pip3 install -r requirements.txt \
    && pip3 install \
        "git+https://github.com/unitreerobotics/unitree_sdk2_python.git@${UNITREE_SDK2_PYTHON_REF}" \
    && SDKDIR=$(python3 -c "import importlib.util as u; print(u.find_spec('unitree_sdk2py').submodule_search_locations[0])") \
    && mkdir -p "$SDKDIR/b2" && touch "$SDKDIR/b2/__init__.py"
# Stub the missing b2/__init__.py (unitree_sdk2_python issues #93/#73). Locate the
# package with find_spec (NOT `import unitree_sdk2py`) — importing it fails with a
# circular-import error precisely because b2/__init__.py doesn't exist yet.

COPY pointcloud2.py app.py cyclonedds.xml ./

ENV FOXGLOVE_PORT={{.FOXGLOVE_PORT}}
ENV INGEST_PORT=8766
ENV LIDAR_TOPIC=rt/utlidar/cloud_deskewed
# Pin the DDS NIC by address (the Go2 Orin is multi-homed).
ENV CYCLONEDDS_URI=file:///app/cyclonedds.xml
EXPOSE {{.FOXGLOVE_PORT}}
EXPOSE 8766

CMD ["python3", "app.py"]
