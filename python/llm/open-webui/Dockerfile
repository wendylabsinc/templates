FROM debian:bookworm-slim

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

ENV PATH="/root/.local/bin:${PATH}" \
    DATA_DIR="/data/open-webui" \
    OLLAMA_BASE_URL="http://ollama:11434" \
    ENABLE_OLLAMA_API="True" \
    ENABLE_OPENAI_API="False" \
    WEBUI_NAME="Wendy" \
    HOST="0.0.0.0" \
    PORT="${PORT}"

RUN mkdir -p /data/open-webui

COPY branding /opt/wendy-brand
COPY entrypoint.sh /usr/local/bin/wendy-webui-entrypoint

RUN chmod 0755 /usr/local/bin/wendy-webui-entrypoint

EXPOSE {{.PORT}}

ENTRYPOINT []
CMD ["wendy-webui-entrypoint"]
