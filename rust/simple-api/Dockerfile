# syntax=docker/dockerfile:1.6
FROM rust:1.85-slim AS builder

WORKDIR /app
COPY Cargo.toml ./
COPY src ./src

# Cache the cargo registry/git downloads (shared across apps) and the target dir
# (per-app). target is a cache mount, so it's absent from the layer — copy the
# binary out to a normal path for the runtime stage to COPY.
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,id=cargo-target-{{.APP_ID}},target=/app/target \
    cargo build --release \
 && cp target/release/{{.APP_ID}} /app/{{.APP_ID}}

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/{{.APP_ID}} /usr/local/bin/{{.APP_ID}}

EXPOSE {{.PORT}}

CMD ["{{.APP_ID}}"]
