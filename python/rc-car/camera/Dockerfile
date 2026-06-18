# rc-car camera — Angstrong Nuwa-HP60C depth camera -> MJPEG bridge (C++).
#
# The HP60C needs the proprietary Angstrong SDK (vendored prebuilt aarch64 .so +
# encrypted config) to initialize and produce RGB frames; raw V4L2/UVC yields
# only black/IR garbage. bridge.cpp drives the SDK and serves multipart MJPEG.
#
# Runs with `network: host`, `usb` (SDK talks USB via libasusb/libasuvc), and
# `camera` entitlements.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ \
        libturbojpeg0-dev \
        libusb-1.0-0 \
        libudev1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Vendored SDK: prebuilt aarch64 libs, headers, and the camera config file.
COPY sdk/include /opt/ascam/include
COPY sdk/lib /opt/ascam/lib
COPY config /opt/ascam/config
COPY bridge.cpp ./

# Link the SDK + the vendored turbojpeg (header comes from libturbojpeg0-dev).
# rpath so the SDK and its sibling .so deps resolve at runtime.
RUN g++ -O2 -std=c++17 bridge.cpp -o camera_bridge \
        -I/opt/ascam/include -L/opt/ascam/lib \
        -lAngstrongCameraSdk -lasuvc -lasusb -lturbojpeg -lpthread \
        -Wl,-rpath,/opt/ascam/lib

ENV LD_LIBRARY_PATH=/opt/ascam/lib
ENV ASCAM_CONFIG=/opt/ascam/config/hp60c_v2_00_20230704_configEncrypt.json
ENV PORT=8000
ENV JPEG_QUALITY=80

EXPOSE 8000

CMD ["./camera_bridge"]
