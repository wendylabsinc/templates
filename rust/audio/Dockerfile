FROM rust:1.85-slim AS builder

RUN apt-get update && apt-get install -y \
    pkg-config \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY Cargo.toml ./
COPY src ./src
COPY index.html ./

RUN cargo build --release

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-alsa \
    alsa-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/target/release/{{.APP_ID}} /app/{{.APP_ID}}
COPY index.html ./
COPY assets ./assets

EXPOSE {{.PORT}}

CMD ["/app/{{.APP_ID}}"]
