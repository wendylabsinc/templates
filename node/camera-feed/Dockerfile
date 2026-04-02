# Stage 1 — build
FROM node:22-slim AS builder

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install

COPY tsconfig.json ./
COPY src ./src
RUN npm run build

# Stage 2 — runtime
FROM node:22-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        v4l-utils && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/dist ./dist
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./
COPY index.html ./
COPY assets ./assets

EXPOSE {{.PORT}}

CMD ["node", "dist/index.js"]
