# syntax=docker/dockerfile:1.6
#
# Pure-Mojo camera streaming: V4L2 capture via libc FFI (wendycam) fanned out
# over hand-rolled WebSockets (wendynet). No GStreamer, no Python at runtime —
# the AOT binary plus the Mojo runtime .so set it links (dynamic linking is
# mandatory today; docs/mojo-max-port-findings.md MMF-009).
FROM python:3.12-slim-bookworm AS builder
# gcc: the Mojo compiler uses the system C compiler as its linker.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir modular==26.5.0
COPY main.mojo /build/
COPY wendynet /build/wendynet
COPY wendycam /build/wendycam
WORKDIR /build
RUN mojo build -I /build -o /build/camera-feed main.mojo
# Collect the SDK runtime libs the binary links so the final stage is slim.
RUN mkdir -p /build/rt \
    && ldd /build/camera-feed | awk '$3 ~ /site-packages/ {print $3}' \
       | xargs -r -I{} cp {} /build/rt/ \
    && ls -la /build/rt

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /build/camera-feed /app/camera-feed
COPY --from=builder /build/rt /app/rt
COPY index.html /app/index.html
COPY assets/ /app/assets/
ENV LD_LIBRARY_PATH=/app/rt
EXPOSE {{.PORT}}
CMD ["/app/camera-feed"]
