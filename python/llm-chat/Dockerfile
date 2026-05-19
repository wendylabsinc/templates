# syntax=docker/dockerfile:1.6
ARG WENDY_PLATFORM=generic
ARG LLAMA_CPP_REF=b8746

# Stage 1 - Build React chat UI.
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
# Use npm install instead of npm ci because the checked-in lockfile can be
# missing platform-specific optional packages required by Linux arm64 builds.
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# Stage 2a - CPU llama.cpp runtime for generic Linux hosts.
FROM python:3.11-slim-bookworm AS base-generic
ARG LLAMA_CPP_REF
RUN set -eux; \
    apt_update() { apt-get update -o Acquire::Retries=5 -o Acquire::http::Timeout=20 -o APT::Update::Error-Mode=any; }; \
    apt_update || { \
        sed -i \
            -e 's|http://ports.ubuntu.com/ubuntu-ports|https://mirror.csclub.uwaterloo.ca/ubuntu-ports|g' \
            -e 's|https://ports.ubuntu.com/ubuntu-ports|https://mirror.csclub.uwaterloo.ca/ubuntu-ports|g' \
            /etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null || true; \
        apt_update; \
    }; \
    apt-get install -y --no-install-recommends \
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
    && rm -rf /opt/llama.cpp

# Stage 2b - Jetson CUDA llama.cpp runtime.
FROM dustynv/pytorch:2.7-r36.4.0-cu128-24.04 AS base-nvidia-jetson
ARG LLAMA_CPP_REF
RUN set -eux; \
    apt_update() { apt-get update -o Acquire::Retries=5 -o Acquire::http::Timeout=20 -o APT::Update::Error-Mode=any; }; \
    apt_update || { \
        sed -i \
            -e 's|http://ports.ubuntu.com/ubuntu-ports|https://mirror.csclub.uwaterloo.ca/ubuntu-ports|g' \
            -e 's|https://ports.ubuntu.com/ubuntu-ports|https://mirror.csclub.uwaterloo.ca/ubuntu-ports|g' \
            /etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null || true; \
        apt_update; \
    }; \
    apt-get install -y --no-install-recommends \
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
        -DLLAMA_CURL=ON \
        -DLLAMA_BUILD_TESTS=OFF \
    && cmake --build /opt/llama.cpp/build -j"$(nproc)" --target llama-server \
    && install -m 0755 /opt/llama.cpp/build/bin/llama-server /usr/local/bin/llama-server \
    && cp -a /opt/llama.cpp/build/bin/lib*.so* /usr/local/lib/ \
    && ldconfig \
    && rm -rf /opt/llama.cpp

# Stage 3 - Wendy app runtime.
#
# Wendy currently reports this Jetson Orin Nano as WENDY_PLATFORM=generic while
# still passing the Jetson device type and NVIDIA GPU metadata. Select the
# Jetson CUDA base directly so the template remains GPU-capable on Orin/Thor.
FROM base-nvidia-jetson AS final
WORKDIR /app

RUN python3 -m venv --system-site-packages /app/venv
ENV PATH="/app/venv/bin:${PATH}" \
    PIP_CONFIG_FILE=/dev/null

COPY requirements.txt .
RUN pip install --no-cache-dir --index-url https://pypi.org/simple -r requirements.txt

ARG PRELOAD_NANO_MODEL=true
RUN if [ "$PRELOAD_NANO_MODEL" = "true" ]; then \
        mkdir -p /opt/wendy/models; \
        python -c "from huggingface_hub import hf_hub_download; hf_hub_download(repo_id='ggml-org/gemma-4-E2B-it-GGUF', filename='gemma-4-E2B-it-Q8_0.gguf', local_dir='/opt/wendy/models')"; \
    fi

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
    LLAMA_REASONING={{.LLAMA_REASONING}} \
    LLAMA_CACHE=/models/llama.cpp \
    PRELOADED_GEMMA_MODEL_PATH=/opt/wendy/models/gemma-4-E2B-it-Q8_0.gguf \
    HF_HOME=/models/huggingface \
    HF_HUB_CACHE=/models/huggingface/hub \
    PYTHONUNBUFFERED=1

RUN if [ "$WENDY_DEBUG" = "true" ]; then pip install --no-cache-dir --index-url https://pypi.org/simple debugpy; fi

EXPOSE {{.PORT}}

CMD ["/app/venv/bin/uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "{{.PORT}}"]
