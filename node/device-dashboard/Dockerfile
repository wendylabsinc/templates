# syntax=docker/dockerfile:1.6
# Stage 1 — Build React frontend
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm install
COPY frontend/ ./
RUN npm run build

# Stage 2 — Build backend TypeScript
FROM node:22-slim AS builder
WORKDIR /app
COPY package*.json ./
RUN --mount=type=cache,target=/root/.npm npm install
COPY tsconfig.json ./
COPY src/ ./src/
RUN npm run build

# Stage 3 — Production runtime
FROM node:22-slim
WORKDIR /app

# Install GStreamer, ALSA, v4l-utils, and native build tools for better-sqlite3
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
    python3 \
    make \
    g++ \
    ccache \
    && rm -rf /var/lib/apt/lists/*

# Install production dependencies (rebuilds better-sqlite3 native addon)
COPY package*.json ./
RUN --mount=type=cache,target=/root/.npm --mount=type=cache,target=/ccache \
    CC="ccache gcc" CXX="ccache g++" CCACHE_DIR=/ccache npm install --omit=dev

# Copy compiled backend and frontend assets
COPY --from=builder /app/dist ./dist
COPY --from=frontend /frontend/dist ./static

EXPOSE {{.PORT}}
CMD ["node", "dist/index.js"]
