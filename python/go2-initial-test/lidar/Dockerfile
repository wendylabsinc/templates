# go2-initial-test — LiDAR test (PointCloud2 over CycloneDDS, no ROS2).
#
# Shares a BYTE-IDENTICAL ubuntu:22.04 + CycloneDDS-from-source prefix with the
# SDK services (lowstate/motion/vui) so buildx compiles CycloneDDS ONCE and
# containerd stores that heavy layer ONCE across all five DDS/SDK services
# (cuts first-build time and on-device disk — review C1/N2). Keep the prefix
# below identical to those Dockerfiles or the shared layer cache misses.
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

ENV PORT=3613
ENV LIDAR_TOPIC=rt/utlidar/cloud_deskewed
# The robot controller IP the app routes to so it can auto-detect this host's own
# DDS bind address at startup (no cyclonedds.xml, no GO2_DDS_ADDRESS prompt). The
# app builds CYCLONEDDS_URI from the detected address; set GO2_DDS_ADDRESS at
# runtime to override the auto-detection.
ENV GO2_IP={{.GO2_IP}}
EXPOSE 3613

CMD ["python3", "app.py"]
