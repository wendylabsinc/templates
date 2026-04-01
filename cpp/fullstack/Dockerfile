# Stage 1: Build frontend
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ .
RUN npm run build

# Stage 2: Build backend
FROM arm64v8/debian:bookworm-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    ca-certificates \
    libjsoncpp-dev \
    libssl-dev \
    uuid-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch v1.9.12 https://github.com/drogonframework/drogon.git /drogon && \
    cd /drogon && \
    git submodule update --init && \
    mkdir build && cd build && \
    cmake .. -DCMAKE_BUILD_TYPE=Release -DBUILD_EXAMPLES=OFF -DBUILD_CTL=OFF -DBUILD_ORM=OFF && \
    make -j$(nproc) && \
    make install && \
    rm -rf /drogon

WORKDIR /app
COPY CMakeLists.txt main.cpp ./
RUN mkdir build && cd build && \
    cmake .. -DCMAKE_BUILD_TYPE=Release && \
    make -j$(nproc)

# Stage 3: Runtime
FROM arm64v8/debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libjsoncpp25 \
    libssl3 \
    libuuid1 \
    zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/build/{{.APP_ID}} .
COPY --from=frontend /frontend/dist ./static

EXPOSE {{.PORT}}
CMD ["./{{.APP_ID}}"]
