# Stage 1: Build the Swift application
FROM swift:6.0-bookworm AS builder
WORKDIR /app
COPY Package.swift ./
COPY Package.resolved ./
RUN swift package resolve
COPY Sources Sources
RUN swift build -c release

# Stage 2: Runtime image with GStreamer and V4L2
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    v4l-utils \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/.build/release/{{.APP_ID}} /usr/local/bin/{{.APP_ID}}
COPY index.html ./index.html
EXPOSE {{.PORT}}
CMD ["{{.APP_ID}}"]
