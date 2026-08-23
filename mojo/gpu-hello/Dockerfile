# syntax=docker/dockerfile:1.6
#
# Pure-Mojo GPU diagnostics. The binary is AOT cross-compiled for the target
# GPU arch (selected from the WENDY_* build args the wendy CLI injects), so the
# final stage needs no Python and no Modular SDK — just the binary plus the
# handful of Mojo runtime .so files it links (dynamic linking is mandatory
# today; see docs/mojo-max-port-findings.md MMF-009). libcuda comes from the
# host driver via the gpu entitlement at runtime, never from the image.
ARG WENDY_PLATFORM=generic
ARG WENDY_DEVICE_TYPE=generic

FROM python:3.12-slim-bookworm AS builder
# gcc: the Mojo compiler uses the system C compiler as its linker.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir modular==26.5.0
COPY main.mojo /build/
WORKDIR /build
ARG WENDY_PLATFORM
ARG WENDY_DEVICE_TYPE
# Jetson Thor is sm_110, every other Jetson (Orin family) is sm_87. Non-Jetson
# targets get a CPU-only build that reports "gpu: not available".
RUN case "${WENDY_PLATFORM}:${WENDY_DEVICE_TYPE}" in \
      nvidia-jetson:*thor*) TARGET="--target-accelerator sm_110" ;; \
      nvidia-jetson:*)      TARGET="--target-accelerator sm_87" ;; \
      *)                    TARGET="" ;; \
    esac \
    && echo "mojo build target: '${TARGET}'" \
    && mojo build ${TARGET} -o /build/gpu-hello main.mojo
# Collect the SDK runtime libs the binary links so the final stage can be slim.
RUN mkdir -p /build/rt \
    && ldd /build/gpu-hello | awk '$3 ~ /site-packages/ {print $3}' \
       | xargs -r -I{} cp {} /build/rt/ \
    && ls -la /build/rt

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /build/gpu-hello /app/gpu-hello
COPY --from=builder /build/rt /app/rt
ENV LD_LIBRARY_PATH=/app/rt
EXPOSE {{.PORT}}
CMD ["/app/gpu-hello"]
