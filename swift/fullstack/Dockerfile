# syntax=docker/dockerfile:1.6
# Stage 1: Build the frontend
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm install
COPY frontend/ .
RUN npm run build

# Stage 2: Build the Swift backend
FROM swift:6.3-bookworm AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    libgstreamer-plugins-bad1.0-dev \
    libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY Package.swift ./
COPY Sources Sources
RUN --mount=type=cache,id=swiftpm-{{.APP_ID}},target=/app/.build \
    swift build -c release \
 && cp .build/release/{{.APP_ID}} /app/{{.APP_ID}}

# Stage 3: Runtime image
FROM swift:6.3-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-alsa \
    alsa-utils \
    v4l-utils \
    sqlite3 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/{{.APP_ID}} /usr/local/bin/{{.APP_ID}}
COPY --from=frontend /frontend/dist ./static
EXPOSE {{.PORT}}
CMD ["{{.APP_ID}}"]
