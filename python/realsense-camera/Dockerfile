# Stage 1 — Build the React/Vite frontend
FROM node:22-alpine AS frontend-builder
WORKDIR /build
COPY package.json package-lock.json ./
# `npm install` not `npm ci` — the upstream sample lockfile may be slightly
# out of sync with package.json (missing @emnapi/* transitive entries on
# some platforms). Regenerate the lockfile (`rm package-lock.json && npm
# install`) and switch this back to `npm ci` once stable.
RUN npm install --no-audit --no-fund
COPY . ./
RUN npm run build


# Stage 2 — Build librealsense + Python bindings from source
# Intel publishes no aarch64 deb for python3-pyrealsense2, so we build it.
FROM ubuntu:22.04 AS librealsense-builder
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      build-essential cmake git pkg-config \
      libusb-1.0-0-dev libssl-dev libudev-dev \
      python3 python3-dev \
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
        -DBUILD_PYTHON_BINDINGS=ON \
        -DPYTHON_EXECUTABLE=/usr/bin/python3 \
        -DFORCE_RSUSB_BACKEND=ON \
    && cmake --build /tmp/librealsense/build -j"$(nproc)" \
    && cmake --install /tmp/librealsense/build \
    && PYDIR=/usr/local/lib/python3.10/dist-packages/pyrealsense2 \
    && mkdir -p "$PYDIR" \
    && find /tmp/librealsense/build -name 'pyrealsense2*.so' -exec cp -v {} "$PYDIR/" \; \
    && find /tmp/librealsense/build -name 'pybackend2*.so'  -exec cp -v {} "$PYDIR/" \; \
    && find /tmp/librealsense -path '*/pyrealsense2/__init__.py' -exec cp -v {} "$PYDIR/" \; \
    && python3 -c "import pyrealsense2 as rs" \
    && rm -rf /tmp/librealsense


# Stage 3 — Runtime
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv \
      libusb-1.0-0 libssl3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=librealsense-builder /usr/local/lib /usr/local/lib
COPY --from=librealsense-builder /usr/local/include/librealsense2 /usr/local/include/librealsense2
RUN ldconfig

RUN python3 -m venv --system-site-packages /app/venv
ENV PATH="/app/venv/bin:$PATH"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Sanity check — fail the build early if the bindings aren't importable.
RUN python3 -c "import pyrealsense2 as rs; print('pyrealsense2 OK')"

COPY server/main.py /app/server/main.py
COPY --from=frontend-builder /build/dist /app/dist

ENV PYTHONPATH=/app/server
ENV FRONTEND_DIST=/app/dist

ARG WENDY_DEVICE_TYPE
ARG WENDY_DEBUG=false
ENV WENDY_DEVICE_TYPE=${WENDY_DEVICE_TYPE}
ENV WENDY_DEBUG=${WENDY_DEBUG}
RUN if [ "$WENDY_DEBUG" = "true" ]; then pip install --no-cache-dir debugpy; fi

EXPOSE {{.PORT}}

CMD ["/app/venv/bin/uvicorn", "main:app", "--host", "0.0.0.0", "--port", "{{.PORT}}"]
