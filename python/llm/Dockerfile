FROM ollama/ollama:latest

ARG OPEN_WEBUI_VERSION=0.9.5
ARG OLLAMA_MODEL={{.OLLAMA_MODEL}}
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

ENV PATH="/root/.local/bin:${PATH}" \
    DATA_DIR="/data/open-webui" \
    OLLAMA_HOST="0.0.0.0:11434" \
    OLLAMA_MODELS="/models/ollama" \
    OLLAMA_MODEL="${OLLAMA_MODEL}" \
    OLLAMA_BASE_URL="http://127.0.0.1:11434" \
    ENABLE_OLLAMA_API="True" \
    ENABLE_OPENAI_API="False" \
    WEBUI_NAME="Wendy" \
    HOST="0.0.0.0" \
    PORT="${PORT}"

RUN mkdir -p /data/open-webui /models/ollama

COPY branding /opt/wendy-brand
COPY entrypoint.sh /usr/local/bin/wendy-llm-entrypoint

RUN chmod 0755 /usr/local/bin/wendy-llm-entrypoint

EXPOSE {{.PORT}} 11434

ENTRYPOINT []
CMD ["wendy-llm-entrypoint"]
