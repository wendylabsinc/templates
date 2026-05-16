# Stage 1: Build the React/Vite frontend.
FROM node:22-alpine AS frontend-builder
WORKDIR /build
COPY package.json package-lock.json ./
# Keep this aligned with the Python RealSense template. The upstream lockfile
# can drift on optional platform packages, so install refreshes it at build time.
RUN npm install --no-audit --no-fund
COPY . ./
RUN npm run build


# Stage 2: Build librealsense, Drogon, and the C++ backend.
FROM ubuntu:22.04 AS backend-builder
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      build-essential cmake git pkg-config \
      libusb-1.0-0-dev libssl-dev libudev-dev \
      libjsoncpp-dev uuid-dev zlib1g-dev \
      libturbojpeg0-dev \
    && rm -rf /var/lib/apt/lists/*

ARG LIBREALSENSE_VERSION=v2.55.1
RUN git clone --depth 1 --branch ${LIBREALSENSE_VERSION} \
      https://github.com/IntelRealSense/librealsense.git /tmp/librealsense \
    && cmake -S /tmp/librealsense -B /tmp/librealsense/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DBUILD_EXAMPLES=OFF \
        -DBUILD_GRAPHICAL_EXAMPLES=OFF \
        -DBUILD_TOOLS=OFF \
        -DBUILD_PYTHON_BINDINGS=OFF \
        -DFORCE_RSUSB_BACKEND=ON \
    && cmake --build /tmp/librealsense/build -j"$(nproc)" \
    && cmake --install /tmp/librealsense/build \
    && rm -rf /tmp/librealsense

RUN git clone --depth 1 --branch v1.9.12 https://github.com/drogonframework/drogon.git /tmp/drogon \
    && cd /tmp/drogon \
    && git submodule update --init \
    && cmake -S . -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_EXAMPLES=OFF \
        -DBUILD_CTL=OFF \
        -DBUILD_ORM=OFF \
    && cmake --build build -j"$(nproc)" \
    && cmake --install build \
    && rm -rf /tmp/drogon

WORKDIR /app
COPY CMakeLists.txt main.cpp ./
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build -j"$(nproc)"


# Stage 3: Runtime.
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      libusb-1.0-0 libssl3 libudev1 \
      libjsoncpp25 libuuid1 zlib1g \
      libturbojpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=backend-builder /usr/local/lib /usr/local/lib
RUN ldconfig

WORKDIR /app
COPY --from=backend-builder /app/build/{{.APP_ID}} .
COPY --from=frontend-builder /build/dist /app/static

ARG WENDY_DEVICE_TYPE
ARG WENDY_DEBUG=false
ENV WENDY_DEVICE_TYPE=${WENDY_DEVICE_TYPE}
ENV WENDY_DEBUG=${WENDY_DEBUG}

EXPOSE {{.PORT}}

CMD ["./{{.APP_ID}}"]
