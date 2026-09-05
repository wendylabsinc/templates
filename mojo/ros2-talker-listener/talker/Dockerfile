# syntax=docker/dockerfile:1.6
#
# Pure-Mojo ROS 2 talker. CycloneDDS is built from source (same 0.10.5 pin
# as the swift sibling — the ROS apt packages target Ubuntu, not Debian),
# but only its runtime library ships: wendydds dlopens libddsc.so.0, so the
# Mojo build stage needs no DDS headers at all. Final stage is the usual
# slim pattern — AOT binary + the Mojo runtime .so set it links (dynamic
# linking is mandatory today; docs/mojo-max-port-findings.md MMF-009).

# Stage 1 — CycloneDDS runtime library
FROM debian:bookworm-slim AS dds
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates cmake git build-essential \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch 0.10.5 \
      https://github.com/eclipse-cyclonedds/cyclonedds.git /tmp/cyclonedds \
 && cmake -S /tmp/cyclonedds -B /tmp/cyclonedds/build \
      -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_BUILD_TYPE=Release \
      -DENABLE_SHM=OFF -DBUILD_TESTING=OFF -DBUILD_EXAMPLES=OFF -DBUILD_DDSPERF=OFF \
 && cmake --build /tmp/cyclonedds/build --target install -j"$(nproc)" \
 && rm -rf /tmp/cyclonedds

# Stage 2 — Mojo build
FROM python:3.12-slim-bookworm AS builder
# gcc: the Mojo compiler uses the system C compiler as its linker.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir modular==26.5.0
COPY main.mojo /build/
COPY wendydds /build/wendydds
WORKDIR /build
RUN mojo build -I /build -o /build/talker main.mojo
# Collect the SDK runtime libs the binary links so the final stage is slim.
RUN mkdir -p /build/rt \
    && ldd /build/talker | awk '$3 ~ /site-packages/ {print $3}' \
       | xargs -r -I{} cp {} /build/rt/ \
    && ls -la /build/rt

# Stage 3 — slim runtime
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=dds /usr/local/lib/libddsc.so* /usr/local/lib/
COPY --from=builder /build/talker /app/talker
COPY --from=builder /build/rt /app/rt
ENV LD_LIBRARY_PATH=/app/rt:/usr/local/lib \
    ROS_DOMAIN_ID={{.ROS_DOMAIN_ID}}
CMD ["/app/talker"]
