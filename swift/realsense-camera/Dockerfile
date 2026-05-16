# Stage 1: Build the React/Vite frontend.
FROM node:22-alpine AS frontend-builder
WORKDIR /build
COPY package.json package-lock.json ./
# Keep this aligned with the other RealSense templates. The upstream lockfile
# can drift on optional platform packages, so install refreshes it at build time.
RUN npm install --no-audit --no-fund
COPY . ./
RUN npm run build


# Stage 2: Build librealsense and the Swift backend with Swift 6.3.2 by default.
FROM swift:{{.SWIFT_VERSION}}-bookworm AS backend-builder
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      build-essential cmake git pkg-config \
      libusb-1.0-0-dev libssl-dev libudev-dev \
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
    && rm -rf /tmp/librealsense \
    && ldconfig

WORKDIR /app
COPY Package.swift .swift-version ./
COPY Sources Sources
RUN swift build -c release \
    && cp .build/release/{{.APP_ID}} /tmp/{{.APP_ID}}


# Stage 3: Runtime.
FROM swift:{{.SWIFT_VERSION}}-bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      libusb-1.0-0 libssl3 libudev1 \
      libturbojpeg0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=backend-builder /usr/local/lib /usr/local/lib
RUN ldconfig

WORKDIR /app
COPY --from=backend-builder /tmp/{{.APP_ID}} ./{{.APP_ID}}
COPY --from=frontend-builder /build/dist /app/static

ARG WENDY_DEVICE_TYPE
ARG WENDY_DEBUG=false
ENV WENDY_DEVICE_TYPE=${WENDY_DEVICE_TYPE}
ENV WENDY_DEBUG=${WENDY_DEBUG}

EXPOSE {{.PORT}}

CMD ["./{{.APP_ID}}"]
