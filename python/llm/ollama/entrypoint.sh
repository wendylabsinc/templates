#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$OLLAMA_MODELS"

# Pull the configured model in the background once the server is up, so the
# API is reachable immediately and the (potentially large) download streams
# its progress to the service log.
(
  until curl -fsS "http://127.0.0.1:11434/api/tags" >/dev/null 2>&1; do
    sleep 2
  done
  if ! OLLAMA_HOST=127.0.0.1:11434 ollama list | awk '{print $1}' | grep -qx "$OLLAMA_MODEL"; then
    echo "Pulling ${OLLAMA_MODEL} into ${OLLAMA_MODELS}"
    OLLAMA_HOST=127.0.0.1:11434 ollama pull "$OLLAMA_MODEL"
  fi
) &

exec ollama serve
