# Stage 1: Build frontend
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ .
RUN npm run build

# Stage 2: Build Rust backend
FROM rust:1.83-slim AS builder
WORKDIR /app
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release

# Stage 3: Final image
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/target/release/{{.APP_ID}} /usr/local/bin/{{.APP_ID}}
COPY --from=frontend /frontend/dist ./static
EXPOSE {{.PORT}}
CMD ["{{.APP_ID}}"]
