# syntax=docker/dockerfile:1.6
#
# The base Ollama image is selected at `wendy run` time from build args the
# wendy CLI injects after querying the target device (see Wendy docs on
# injected build args):
#   WENDY_GPU_VENDOR=nvidia + WENDY_JETPACK_MAJOR=7
#       -> NVIDIA's JetPack-7 / CUDA-13 SBSA build (Thor, Orin on JetPack 7)
#   WENDY_GPU_VENDOR=nvidia + WENDY_JETPACK_MAJOR=6
#       -> stock ollama/ollama with its bundled JetPack-6 CUDA libs forced on
#   anything else (x86_64 CUDA where present, otherwise CPU, and plain
#   `docker build` with no args) -> stock ollama/ollama
#
# Unlike the camera-feed-yolo templates, no CUDA-12-runtime/LD_LIBRARY_PATH
# shim is needed here: that fix is for JetPack-6 images running on JetPack-7
# hosts, whereas these bases are matched to their host's JetPack generation.
ARG WENDY_GPU_VENDOR=generic
ARG WENDY_JETPACK_MAJOR=0

# Stock image: CUDA on x86_64 where present, CPU otherwise.
FROM ollama/ollama:latest AS base-generic

# JetPack 7 (L4T r38 / CUDA 13, SBSA arm64): stock ollama ships no compatible
# CUDA backend and falls back to CPU, so use NVIDIA's JetPack-7 build.
FROM ghcr.io/nvidia-ai-iot/ollama:r38.2.arm64-sbsa-cu130-24.04 AS base-nvidia-jp7

# JetPack 6 (Orin on L4T r36): recent official arm64 ollama images bundle
# JetPack CUDA libs, but /etc/nv_tegra_release is not visible in-container,
# so force the JetPack-6 backend explicitly.
FROM ollama/ollama:latest AS base-nvidia-jp6
ENV JETSON_JETPACK=6

# Alias stages so every WENDY_GPU_VENDOR x WENDY_JETPACK_MAJOR combination
# resolves (jp0 = arg not injected, e.g. x86 NVIDIA dGPU or plain docker build).
FROM base-generic AS base-nvidia-jp0
FROM base-generic AS base-generic-jp0
FROM base-generic AS base-generic-jp6
FROM base-generic AS base-generic-jp7

FROM base-${WENDY_GPU_VENDOR}-jp${WENDY_JETPACK_MAJOR} AS final

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

ENV OLLAMA_HOST="0.0.0.0:11434" \
    OLLAMA_MODELS="/models/ollama" \
    OLLAMA_MODEL="gemma4:e2b"

RUN mkdir -p /models/ollama

COPY entrypoint.sh /usr/local/bin/wendy-ollama-entrypoint
RUN chmod 0755 /usr/local/bin/wendy-ollama-entrypoint

EXPOSE 11434

ENTRYPOINT []
CMD ["wendy-ollama-entrypoint"]
