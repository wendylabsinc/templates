FROM swift:6.0-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY Package.swift .
COPY Sources/ Sources/
RUN swift build -c release

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgstreamer1.0-0 \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-alsa \
    alsa-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/.build/release/{{.APP_ID}} /app/{{.APP_ID}}
COPY index.html /app/index.html
COPY assets/ /app/assets/

EXPOSE {{.PORT}}

ENTRYPOINT ["/app/{{.APP_ID}}"]
