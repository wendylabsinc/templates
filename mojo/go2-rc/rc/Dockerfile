# syntax=docker/dockerfile:1.6
ARG WENDY_PLATFORM=generic
ARG WENDY_DEVICE_TYPE=generic

FROM python:3.12-slim-bookworm AS builder
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir modular==26.5.0
COPY main.mojo /build/
COPY wendynet /build/wendynet
WORKDIR /build
ARG WENDY_PLATFORM
ARG WENDY_DEVICE_TYPE
RUN case "${WENDY_PLATFORM}:${WENDY_DEVICE_TYPE}" in \
      nvidia-jetson:*thor*) TARGET="--target-accelerator sm_110" ;; \
      nvidia-jetson:*)      TARGET="--target-accelerator sm_87" ;; \
      *)                    TARGET="" ;; \
    esac \
    && echo "mojo build target: '${TARGET}'" \
    && mojo build ${TARGET} -I /build -o /build/go2-rc main.mojo
RUN mkdir -p /build/rt \
    && ldd /build/go2-rc | awk '$3 ~ /site-packages/ {print $3}' \
       | xargs -r -I{} cp {} /build/rt/

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /build/go2-rc /app/go2-rc
COPY --from=builder /build/rt /app/rt
COPY index.html /app/index.html
COPY assets/ /app/assets/
ENV LD_LIBRARY_PATH=/app/rt \
    PORT={{.RC_PORT}}
EXPOSE {{.RC_PORT}}
CMD ["/app/go2-rc"]
