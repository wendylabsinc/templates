#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$OLLAMA_MODELS"

# Pull the configured model in the background once the server is up, so the
# API is reachable immediately and the (potentially large) download streams
# its progress to the service log. Keep retrying because first boot often races
# network readiness; without retries Open WebUI can stay up with an empty model
# picker forever after a transient DNS or connectivity failure.
(
  export OLLAMA_HOST=127.0.0.1:11434

  echo "Waiting for Ollama API before pulling ${OLLAMA_MODEL}..."
  until curl -fsS "http://127.0.0.1:11434/api/tags" >/dev/null 2>&1; do
    sleep 2
  done

  retry_delay=10
  max_retry_delay=60

  while true; do
    if ollama list | awk 'NR > 1 { print $1 }' | grep -qx "$OLLAMA_MODEL"; then
      echo "Ollama model ${OLLAMA_MODEL} is available in ${OLLAMA_MODELS}."
      break
    fi

    echo "First-run setup: pulling ${OLLAMA_MODEL} into ${OLLAMA_MODELS}."
    echo "Open WebUI may show an empty model list until this download completes."

    if ollama pull "$OLLAMA_MODEL"; then
      echo "Finished pulling ${OLLAMA_MODEL}."
      retry_delay=10
      continue
    fi

    echo "Pulling ${OLLAMA_MODEL} failed. Network/DNS may not be ready yet, or the model name may be unavailable."
    echo "Retrying in ${retry_delay}s..."
    sleep "$retry_delay"

    retry_delay=$((retry_delay * 2))
    if ((retry_delay > max_retry_delay)); then
      retry_delay=$max_retry_delay
    fi
  done
) &

exec ollama serve
