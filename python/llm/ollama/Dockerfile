FROM ollama/ollama:latest

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
