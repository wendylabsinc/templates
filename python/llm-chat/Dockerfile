# syntax=docker/dockerfile:1.6
ARG WENDY_PLATFORM=generic
ARG LLAMA_CPP_REF=b8746

# Stage 1 - Build React chat UI.
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2a - CPU llama.cpp runtime for generic Linux hosts.
FROM python:3.11-slim-bookworm AS base-generic
ARG LLAMA_CPP_REF
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        curl \
        git \
        libcurl4-openssl-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${LLAMA_CPP_REF}" https://github.com/ggml-org/llama.cpp.git /opt/llama.cpp \
    && cmake -S /opt/llama.cpp -B /opt/llama.cpp/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_NATIVE=OFF \
        -DLLAMA_CURL=ON \
        -DLLAMA_BUILD_TESTS=OFF \
    && cmake --build /opt/llama.cpp/build -j"$(nproc)" --target llama-server \
    && cmake --install /opt/llama.cpp/build --prefix /usr/local \
    && llama-server --help >/dev/null \
    && rm -rf /opt/llama.cpp

# Stage 2b - Jetson CUDA llama.cpp runtime.
FROM dustynv/pytorch:2.7-r36.4.0-cu128-24.04 AS base-nvidia-jetson
ARG LLAMA_CPP_REF
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        curl \
        git \
        libcurl4-openssl-dev \
        pkg-config \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${LLAMA_CPP_REF}" https://github.com/ggml-org/llama.cpp.git /opt/llama.cpp \
    && cmake -S /opt/llama.cpp -B /opt/llama.cpp/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_NATIVE=OFF \
        -DGGML_CUDA=ON \
        -DGGML_CUDA_FA_ALL_QUANTS=ON \
        -DLLAMA_CURL=ON \
        -DLLAMA_BUILD_TESTS=OFF \
    && cmake --build /opt/llama.cpp/build -j"$(nproc)" --target llama-server \
    && cmake --install /opt/llama.cpp/build --prefix /usr/local \
    && llama-server --help >/dev/null \
    && rm -rf /opt/llama.cpp

# Stage 3 - Wendy app runtime.
FROM base-${WENDY_PLATFORM} AS final
WORKDIR /app

RUN python3 -m venv --system-site-packages /app/venv
ENV PATH="/app/venv/bin:${PATH}"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ ./server/
COPY --from=frontend /frontend/dist ./static

ARG WENDY_PLATFORM=generic
ARG WENDY_DEVICE_TYPE=""
ARG WENDY_HAS_GPU=""
ARG WENDY_GPU_VENDOR=""
ARG WENDY_JETPACK_VERSION=""
ARG WENDY_CUDA_VERSION=""
ARG WENDY_DEBUG=false
ENV WENDY_PLATFORM=${WENDY_PLATFORM} \
    WENDY_DEVICE_TYPE=${WENDY_DEVICE_TYPE} \
    WENDY_HAS_GPU=${WENDY_HAS_GPU} \
    WENDY_GPU_VENDOR=${WENDY_GPU_VENDOR} \
    WENDY_JETPACK_VERSION=${WENDY_JETPACK_VERSION} \
    WENDY_CUDA_VERSION=${WENDY_CUDA_VERSION} \
    WENDY_DEBUG=${WENDY_DEBUG} \
    APP_ID={{.APP_ID}} \
    PORT={{.PORT}} \
    GEMMA_MODEL={{.GEMMA_MODEL}} \
    LLAMA_CONTEXT_SIZE={{.LLAMA_CONTEXT_SIZE}} \
    LLAMA_GPU_LAYERS={{.LLAMA_GPU_LAYERS}} \
    LLAMA_THREADS={{.LLAMA_THREADS}} \
    LLAMA_CACHE=/models/llama.cpp \
    HF_HOME=/models/huggingface \
    HF_HUB_CACHE=/models/huggingface/hub \
    PYTHONUNBUFFERED=1

RUN if [ "$WENDY_DEBUG" = "true" ]; then pip install --no-cache-dir debugpy; fi

EXPOSE {{.PORT}}

CMD ["/app/venv/bin/uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "{{.PORT}}"]
