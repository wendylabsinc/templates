# go2-initial-test — speaker test (publish a tone to rt/audioreceiver over DDS).
#
# Shares a BYTE-IDENTICAL ubuntu:22.04 + CycloneDDS-from-source prefix with the
# SDK services (lowstate/motion/vui) and lidar so CycloneDDS is compiled once and
# the layer is reused across all five (review C1/N2). Keep the prefix identical.
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

ENV PORT=3615
ENV AUDIO_OUT_TOPIC=rt/audioreceiver
# pcm16 was the empirically-working codec in the go2-camera demo (ulaw = noise);
# flip to ulaw/alaw if the tone is silent on your firmware.
ENV AUDIO_OUT_CODEC=pcm16
# Robot controller IP the app routes to so it can auto-detect this host's DDS bind
# address at startup (no cyclonedds.xml). Set GO2_DDS_ADDRESS at runtime to override.
ENV GO2_IP={{.GO2_IP}}
EXPOSE 3615

CMD ["python3", "app.py"]
