# Build Swift server with GStreamer
FROM swift:6.2.3-noble AS swift-builder

# Install GStreamer development packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY Package.swift ./
COPY Server ./Server

RUN swift build

# Runtime stage
FROM swift:6.2.3-noble-slim

# Install GStreamer runtime and video utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgstreamer1.0-0 \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    v4l-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the built Swift binary
COPY --from=swift-builder /app/.build/debug/WebcamServer /usr/local/bin/webcam-server

# Copy static files
COPY index.html .
COPY logo.svg .

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash app && \
    chown -R app:app /app && \
    usermod -aG video app
USER app

EXPOSE {{.PORT}}

CMD ["webcam-server"]
