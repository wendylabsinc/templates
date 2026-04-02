# Stage 1: Build
FROM rust:1.83-slim AS builder

RUN apt-get update && apt-get install -y \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY index.html ./
COPY assets ./assets

RUN cargo build --release

# Stage 2: Runtime
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    v4l-utils \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/target/release/{{.APP_ID}} /usr/local/bin/{{.APP_ID}}
COPY --from=builder /app/index.html /usr/local/bin/index.html
COPY --from=builder /app/assets /usr/local/bin/assets

EXPOSE {{.PORT}}

CMD ["{{.APP_ID}}"]
