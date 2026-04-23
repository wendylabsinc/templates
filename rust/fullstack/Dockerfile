# Stage 1: Build frontend
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ .
RUN npm run build

# Stage 2: Build Rust backend
FROM rust:1.85-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    libgstreamer-plugins-bad1.0-dev \
    libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY Cargo.toml ./
# Create a dummy main.rs so dependencies can be cached
RUN mkdir src && echo "fn main() {}" > src/main.rs
RUN cargo build --release || true
# Now copy the real source and build
COPY src ./src
RUN touch src/main.rs && cargo build --release

# Stage 3: Final image
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-alsa \
    libgstreamer1.0-0 \
    libgstreamer-plugins-base1.0-0 \
    alsa-utils \
    v4l-utils \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/target/release/{{.APP_ID}} /usr/local/bin/{{.APP_ID}}
COPY --from=frontend /frontend/dist ./static
EXPOSE {{.PORT}}
CMD ["{{.APP_ID}}"]
