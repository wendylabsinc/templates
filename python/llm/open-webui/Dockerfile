# syntax=docker/dockerfile:1.6
#
# The base Ollama image is selected at `wendy run` time from the WENDY_GPU_VENDOR
# build arg the wendy CLI injects after querying the target device (see Wendy
# docs on injected build args):
#   nvidia  -> NVIDIA's CUDA Ollama build (GPU-accelerated)
#   <unset> -> stock ollama/ollama (x86_64 CUDA where present, otherwise CPU)
# Falls back to the stock image for plain `docker build` (no build arg supplied).
#
# The stock ollama/ollama image ships no CUDA backend compatible with Jetson AGX
# Thor (L4T r38 / JetPack 7 / CUDA 13, SBSA arm64) and falls back to CPU there,
# so the nvidia path uses NVIDIA's Thor build. That image is currently the only
# NVIDIA path wired up here; other NVIDIA devices (e.g. Jetson Orin / JetPack 6)
# are not yet special-cased and would also receive it.
ARG WENDY_GPU_VENDOR=generic

FROM ollama/ollama:latest AS base-generic
FROM ghcr.io/nvidia-ai-iot/ollama:r38.2.arm64-sbsa-cu130-24.04 AS base-nvidia

FROM base-${WENDY_GPU_VENDOR} AS final

ARG OPEN_WEBUI_VERSION=0.9.5
ARG PORT={{.PORT}}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    python3 \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && /root/.local/bin/uv python install 3.11 \
    && /root/.local/bin/uv tool install "open-webui==${OPEN_WEBUI_VERSION}" --python 3.11

# numpy/OpenBLAS in the open-webui deps do runtime CPU-feature detection and can
# select instructions the target ARM core lacks, crashing `import numpy` with
# SIGILL (exit 132) on first launch on-device. Pin OpenBLAS to the generic ARMv8
# baseline so it loads on any arm64 core; costs some BLAS throughput, but
# open-webui isn't doing heavy math.
ENV PATH="/root/.local/bin:${PATH}" \
    OPENBLAS_CORETYPE="ARMV8" \
    DATA_DIR="/data/open-webui" \
    OLLAMA_BASE_URL="http://ollama:11434" \
    ENABLE_OLLAMA_API="True" \
    ENABLE_OPENAI_API="False" \
    WEBUI_NAME="Wendy" \
    HOST="0.0.0.0" \
    PORT="${PORT}"

# Fail the build early if open-webui's native deps can't execute on the build
# CPU, instead of shipping an image that SIGILLs (exit 132) on first launch.
# This validates the OPENBLAS_CORETYPE pin above. Note: under cross-arch
# emulation the emulated CPU may differ from the device, so this is a guard
# against incompatible wheels, not a guarantee for every target core.
RUN /root/.local/share/uv/tools/open-webui/bin/python -c "import numpy; print('numpy', numpy.__version__, 'imports OK')"

RUN mkdir -p /data/open-webui /models/ollama

COPY branding /opt/wendy-brand
COPY entrypoint.sh /usr/local/bin/wendy-webui-entrypoint

RUN chmod 0755 /usr/local/bin/wendy-webui-entrypoint

EXPOSE {{.PORT}}

ENTRYPOINT []
CMD ["wendy-webui-entrypoint"]
