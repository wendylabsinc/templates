# syntax=docker/dockerfile:1.6
#
# Fullstack: React frontend (unchanged from the python sibling) + pure-Mojo
# backend. The backend AOT-compiles with a GPU target on Jetson so /api/gpu
# runs a real Mojo kernel; the final stage is slim debian — no Python, no
# Modular SDK, just the binary, the Mojo runtime .so set it links (dynamic
# linking is mandatory today; docs/mojo-max-port-findings.md MMF-009), and
# the C libraries wendyaudio/wendydb dlopen at runtime. libcuda comes from
# the host driver via the gpu entitlement, never from the image.
ARG WENDY_PLATFORM=generic
ARG WENDY_DEVICE_TYPE=generic

# Stage 1 — build the React frontend
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm install
COPY frontend/ ./
RUN npm run build

# Stage 2 — build the Mojo backend
FROM python:3.12-slim-bookworm AS builder
# gcc: the Mojo compiler uses the system C compiler as its linker.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir modular==26.5.0
COPY main.mojo carstore.mojo sysinfo.mojo gpudiag.mojo /build/
COPY wendynet /build/wendynet
COPY wendycam /build/wendycam
COPY wendyaudio /build/wendyaudio
COPY wendydb /build/wendydb
WORKDIR /build
ARG WENDY_PLATFORM
ARG WENDY_DEVICE_TYPE
# Jetson Thor is sm_110, every other Jetson (Orin family) is sm_87. Non-Jetson
# targets get a CPU-only build whose /api/gpu falls back to thermal-zone info.
RUN case "${WENDY_PLATFORM}:${WENDY_DEVICE_TYPE}" in \
      nvidia-jetson:*thor*) TARGET="--target-accelerator sm_110" ;; \
      nvidia-jetson:*)      TARGET="--target-accelerator sm_87" ;; \
      *)                    TARGET="" ;; \
    esac \
    && echo "mojo build target: '${TARGET}'" \
    && mojo build ${TARGET} -I /build -o /build/fullstack main.mojo
# Collect the SDK runtime libs the binary links so the final stage is slim.
RUN mkdir -p /build/rt \
    && ldd /build/fullstack | awk '$3 ~ /site-packages/ {print $3}' \
       | xargs -r -I{} cp {} /build/rt/ \
    && ls -la /build/rt

# Stage 3 — slim runtime
FROM debian:bookworm-slim
# libasound2: wendyaudio dlopens libasound.so.2; libsqlite3-0: wendydb
# dlopens libsqlite3.so.0.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libstdc++6 libasound2 libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /build/fullstack /app/fullstack
COPY --from=builder /build/rt /app/rt
COPY --from=frontend /frontend/dist /app/static
ENV LD_LIBRARY_PATH=/app/rt \
    PORT={{.PORT}}
EXPOSE {{.PORT}}
CMD ["/app/fullstack"]
