# syntax=docker/dockerfile:1.6
#
# Frontend only — needs no GPU. Inference runs in ../max-serve, which exposes
# an OpenAI-compatible /v1 API on 9011; this container just points Open WebUI
# at it.
FROM ubuntu:24.04

ARG OPEN_WEBUI_VERSION=0.9.5
ARG PORT={{.PORT}}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    python3 \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=cache,target=/root/.cache/uv \
    curl -LsSf https://astral.sh/uv/install.sh | sh \
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
    OPENAI_API_BASE_URL="http://max-serve:9011/v1" \
    OPENAI_API_KEY="wendy-local" \
    ENABLE_OLLAMA_API="False" \
    ENABLE_OPENAI_API="True" \
    WEBUI_NAME="Wendy" \
    HOST="0.0.0.0" \
    PORT="${PORT}"

# Fail the build early if open-webui's native deps can't execute on the build
# CPU, instead of shipping an image that SIGILLs (exit 132) on first launch.
# This validates the OPENBLAS_CORETYPE pin above. Note: under cross-arch
# emulation the emulated CPU may differ from the device, so this is a guard
# against incompatible wheels, not a guarantee for every target core.
RUN /root/.local/share/uv/tools/open-webui/bin/python -c "import numpy; print('numpy', numpy.__version__, 'imports OK')"

RUN mkdir -p /data/open-webui

COPY branding /opt/wendy-brand
COPY entrypoint.sh /usr/local/bin/wendy-webui-entrypoint

RUN chmod 0755 /usr/local/bin/wendy-webui-entrypoint

EXPOSE {{.PORT}}

ENTRYPOINT []
CMD ["wendy-webui-entrypoint"]
