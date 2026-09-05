# syntax=docker/dockerfile:1.6
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY frontend/ ./
RUN npm run build

FROM swift:6.3-bookworm AS builder

WORKDIR /app
COPY Package.swift ./
COPY Sources Sources
# Cache the SwiftPM build dir (per-app). .build is a cache mount, so it's absent
# from the layer — copy the binary out to a normal path for the runtime stage.
RUN --mount=type=cache,id=swiftpm-{{.APP_ID}},target=/app/.build \
    swift build -c release \
 && cp .build/release/{{.APP_ID}} /app/{{.APP_ID}}

FROM swift:6.3-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/{{.APP_ID}} /usr/local/bin/{{.APP_ID}}

WORKDIR /app
COPY --from=frontend /frontend/dist ./static

EXPOSE {{.PORT}}

CMD ["{{.APP_ID}}"]
