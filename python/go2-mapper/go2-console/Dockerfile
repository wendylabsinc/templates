# go2-console — no ROS needed: talks DDS directly via cyclonedds-python
# (same trick as go2-foxglove's bridge) and pure-numpy export math.
#
# CycloneDDS-python has no arm64 wheel, so `pip install cyclonedds` builds from
# source and needs the native lib present (CYCLONEDDS_HOME). Same proven base as
# go2-foxglove: ubuntu:22.04 + CycloneDDS 0.10.5 from source, then pin
# cyclonedds<0.11 (>=0.11 needs a newer C lib and fails with a missing
# ddsi_radmin.h). `pip install cyclonedds` on python:3.11-slim does NOT work.
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1

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

# scipy: cKDTree for the offline loop-closure refine (refine.py, robustness #2).
RUN pip3 install --no-cache-dir \
        fastapi==0.115.6 "uvicorn[standard]==0.34.0" httpx numpy scipy pillow "cyclonedds<0.11"
WORKDIR /opt/console
COPY app.py export.py nogo.py quality.py refine.py ./
COPY static ./static
COPY cyclonedds.xml /opt/cyclonedds.xml

# Runtime config (baked at scaffold time from template.json — no compose file).
ENV CYCLONEDDS_URI=/opt/cyclonedds.xml \
    PORT={{.CONSOLE_PORT}} \
    RECORDER_URL=http://127.0.0.1:3610 \
    MAP_SAVE_DIR=/data/maps \
    EXPORT_DIR=/data/exports \
    FOXGLOVE_PORT={{.FOXGLOVE_PORT}} \
    SLICE_MIN_M={{.SLICE_MIN_M}} \
    SLICE_MAX_M={{.SLICE_MAX_M}} \
    GRID_RESOLUTION_M={{.GRID_RESOLUTION_M}} \
    LIDAR_HEIGHT_OFFSET_M={{.LIDAR_HEIGHT_OFFSET_M}} \
    EXPORT_VOXEL_M=0.03

EXPOSE {{.CONSOLE_PORT}}
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-3600}"]
