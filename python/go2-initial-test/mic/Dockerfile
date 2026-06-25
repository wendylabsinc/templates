# go2-initial-test — microphone test (Go2 mic over CycloneDDS, no ROS2).
#
# The Go2's mic publishes G.711 µ-law on rt/audiosender (unitree_go AudioData) —
# it is NOT a local ALSA device, so this subscribes over DDS (the old sounddevice
# capture could never work on the dog). Shares a BYTE-IDENTICAL ubuntu:22.04 +
# CycloneDDS-from-source prefix with lidar/speaker/lowstate/motion/vui so buildx
# compiles CycloneDDS ONCE and the heavy layer is reused. Keep the prefix below
# identical to those Dockerfiles or the shared layer cache misses.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-dev git build-essential cmake pkg-config ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG CYCLONEDDS_VERSION=0.10.5
RUN git clone --depth 1 --branch ${CYCLONEDDS_VERSION} \
        https://github.com/eclipse-cyclonedds/cyclonedds.git /tmp/cyclonedds \
    && cmake -S /tmp/cyclonedds -B /tmp/cyclonedds/build \
        -DCMAKE_INSTALL_PREFIX=/usr/local -DBUILD_EXAMPLES=OFF -DBUILD_TESTING=OFF -DBUILD_IDLC=ON \
    && cmake --build /tmp/cyclonedds/build -j"$(nproc)" \
    && cmake --install /tmp/cyclonedds/build && ldconfig && rm -rf /tmp/cyclonedds
ENV CYCLONEDDS_HOME=/usr/local

WORKDIR /app
COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt
COPY app.py ./

ENV PORT=3614
ENV MIC_TOPIC=rt/audiosender
# Robot controller IP to auto-detect this host's DDS bind address at startup;
# set GO2_DDS_ADDRESS at runtime to override.
ENV GO2_IP={{.GO2_IP}}
EXPOSE 3614

CMD ["python3", "app.py"]
