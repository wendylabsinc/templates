# syntax=docker/dockerfile:1.6
#
# Pure-Mojo REST API. The final stage is debian-slim + the binary + the ~2 MB
# of Mojo runtime .so files it links (dynamic linking is mandatory today;
# docs/mojo-max-port-findings.md MMF-009).
FROM python:3.12-slim-bookworm AS builder
# gcc: the Mojo compiler uses the system C compiler as its linker.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir mojo==1.0.0
COPY main.mojo /build/
COPY wendynet /build/wendynet
WORKDIR /build
RUN mojo build -o /build/api main.mojo
RUN mkdir -p /build/rt \
    && ldd /build/api | awk '$3 ~ /site-packages/ {print $3}' \
       | xargs -r -I{} cp {} /build/rt/ \
    && ls -la /build/rt

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /build/api /app/api
COPY --from=builder /build/rt /app/rt
ENV LD_LIBRARY_PATH=/app/rt \
    PORT={{.PORT}}
EXPOSE {{.PORT}}
CMD ["/app/api"]
