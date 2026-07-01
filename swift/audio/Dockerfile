# syntax=docker/dockerfile:1.6
FROM swift:{{.SWIFT_VERSION}}-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY Package.swift .
COPY Sources/ Sources/
RUN --mount=type=cache,id=swiftpm-{{.APP_ID}},target=/app/.build \
    swift build -c release \
 && cp .build/release/{{.APP_ID}} /app/{{.APP_ID}}

FROM swift:{{.SWIFT_VERSION}}-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgstreamer1.0-0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-alsa \
    alsa-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/{{.APP_ID}} /usr/local/bin/{{.APP_ID}}
COPY index.html /app/index.html
COPY assets/ /app/assets/

EXPOSE {{.PORT}}

CMD ["{{.APP_ID}}"]
