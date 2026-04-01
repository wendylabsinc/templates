# Stage 1: Build the frontend
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ .
RUN npm run build

# Stage 2: Build the Swift backend
FROM swift:6.0-bookworm AS builder
WORKDIR /app
COPY Package.swift ./
COPY Package.resolved ./
RUN swift package resolve
COPY Sources Sources
RUN swift build -c release

# Stage 3: Runtime image
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/.build/release/{{.APP_ID}} /usr/local/bin/{{.APP_ID}}
COPY --from=frontend /frontend/dist ./static
EXPOSE {{.PORT}}
CMD ["{{.APP_ID}}"]
