# Stage 1: Build the Swift application
FROM swift:{{.SWIFT_VERSION}}-bookworm AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    libgstreamer-plugins-bad1.0-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY Package.swift ./
COPY Sources Sources
RUN swift build -c release

# Stage 2: Runtime image with GStreamer and V4L2
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    v4l-utils \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/.build/release/{{.APP_ID}} /usr/local/bin/{{.APP_ID}}
COPY index.html ./index.html
COPY assets/ ./assets/
EXPOSE {{.PORT}}
CMD ["{{.APP_ID}}"]
