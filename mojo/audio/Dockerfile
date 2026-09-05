# syntax=docker/dockerfile:1.6
#
# Pure-Mojo audio streaming: ALSA capture/playback via libasound dlopen
# (wendyaudio) over hand-rolled WebSockets (wendynet). No GStreamer, no
# Python at runtime — the AOT binary plus the Mojo runtime .so set it links
# (dynamic linking is mandatory today; docs/mojo-max-port-findings.md MMF-009).
FROM python:3.12-slim-bookworm AS builder
# gcc: the Mojo compiler uses the system C compiler as its linker.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir modular==26.5.0
COPY main.mojo /build/
COPY wendynet /build/wendynet
COPY wendyaudio /build/wendyaudio
WORKDIR /build
RUN mojo build -I /build -o /build/audio main.mojo
# Collect the SDK runtime libs the binary links so the final stage is slim.
RUN mkdir -p /build/rt \
    && ldd /build/audio | awk '$3 ~ /site-packages/ {print $3}' \
       | xargs -r -I{} cp {} /build/rt/ \
    && ls -la /build/rt

FROM debian:bookworm-slim
# libasound2: wendyaudio dlopens libasound.so.2 at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 libasound2 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /build/audio /app/audio
COPY --from=builder /build/rt /app/rt
COPY index.html /app/index.html
COPY assets/ /app/assets/
ENV LD_LIBRARY_PATH=/app/rt \
    PORT={{.PORT}}
EXPOSE {{.PORT}}
CMD ["/app/audio"]
