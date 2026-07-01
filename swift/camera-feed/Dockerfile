# syntax=docker/dockerfile:1.6
# Stage 1: Build the Swift application
FROM swift:{{.SWIFT_VERSION}}-bookworm AS builder
WORKDIR /app
COPY Package.swift ./
COPY Sources Sources
RUN --mount=type=cache,id=swiftpm-{{.APP_ID}},target=/app/.build \
    swift build -c release \
 && cp .build/release/{{.APP_ID}} /app/{{.APP_ID}}

# Stage 2: Runtime image with GStreamer and V4L2
FROM swift:{{.SWIFT_VERSION}}-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    v4l-utils \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/{{.APP_ID}} /usr/local/bin/{{.APP_ID}}
COPY index.html ./index.html
COPY assets/ ./assets/
EXPOSE {{.PORT}}
CMD ["{{.APP_ID}}"]
