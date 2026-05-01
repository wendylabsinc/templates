#!/bin/bash
# Container entrypoint: start the Ollama daemon, ensure the configured
# model is present locally, then hand off to the Pipecat app.
#
# Ollama runs as a background daemon for the lifetime of the container.
# Pipecat (main.py) talks to it via http://localhost:11434/v1 only
# when the user has selected the `ollama` provider in /api/settings —
# until then Ollama is idle and the cloud LLMs work as before.
#
# Model handling: this is the "first-boot pull" variant. We pull the
# model from Ollama's registry on container start if it's not already
# cached. That means the model isn't baked into the image (smaller
# image, faster rebuilds during iteration) but the first boot needs
# the dog to have an internet route so the pull succeeds. Once the
# Dockerfile is proven, we can move the pull into a `RUN` step at
# build time for instant first-boot.

set -uo pipefail

OLLAMA_LOG=/tmp/ollama.log
OLLAMA_HOST=${OLLAMA_HOST:-http://localhost:11434}
# main.py reads this on startup and surfaces it on /api/status as
# `local_llm_load_error`, so a failed model pull shows in the UI as a
# banner instead of silently breaking every LLM turn at runtime.
LOCAL_LLM_ERROR_FILE=${LOCAL_LLM_ERROR_FILE:-/tmp/voice-ai-local-llm-error.txt}
rm -f "$LOCAL_LLM_ERROR_FILE"

echo "[entrypoint] starting ollama daemon (logs → $OLLAMA_LOG)"
# Daemon must run on 0.0.0.0 inside the container so the gpu
# entitlement's network namespace doesn't cut it off from itself.
# Default bind is 127.0.0.1 which is fine for our case (Pipecat is
# in the same container) but explicit beats implicit here.
OLLAMA_HOST=0.0.0.0:11434 ollama serve > "$OLLAMA_LOG" 2>&1 &
OLLAMA_PID=$!

# Wait for the daemon to start responding. 30 s upper bound — if it
# isn't up by then something is wrong (no CUDA libs visible, port
# already bound, etc.) and we should surface it.
echo "[entrypoint] waiting for ollama API to come up"
for i in $(seq 1 30); do
  if curl -sf "$OLLAMA_HOST/api/tags" >/dev/null 2>&1; then
    echo "[entrypoint] ollama is up after ${i}s"
    break
  fi
  if ! kill -0 "$OLLAMA_PID" 2>/dev/null; then
    echo "[entrypoint] ERROR: ollama daemon died during startup. Last log lines:"
    tail -20 "$OLLAMA_LOG" || true
    exit 1
  fi
  sleep 1
done

if ! curl -sf "$OLLAMA_HOST/api/tags" >/dev/null 2>&1; then
  echo "[entrypoint] ERROR: ollama did not come up within 30 s. Last log lines:"
  tail -20 "$OLLAMA_LOG" || true
  exit 1
fi

# Pull the configured model if it isn't already in Ollama's local
# registry. The container still starts on pull failure (the user can
# still hit cloud LLMs via /api/settings) but the failure is recorded
# so the UI can show a banner instead of pretending the local LLM is
# fine.
if [ -n "${LOCAL_LLM_MODEL:-}" ]; then
  cached=0
  if list_out=$(ollama list 2>&1); then
    if printf '%s\n' "$list_out" | awk 'NR>1 {print $1}' | grep -qx "$LOCAL_LLM_MODEL"; then
      cached=1
    fi
  else
    echo "[entrypoint] WARN: 'ollama list' failed, treating model as not cached: $list_out"
  fi
  if [ "$cached" = "1" ]; then
    echo "[entrypoint] $LOCAL_LLM_MODEL already cached, skipping pull"
  else
    echo "[entrypoint] pulling $LOCAL_LLM_MODEL — first boot, expect 1–3 min on WiFi"
    if pull_out=$(ollama pull "$LOCAL_LLM_MODEL" 2>&1); then
      echo "[entrypoint] $LOCAL_LLM_MODEL pulled successfully"
    else
      printf 'Local LLM model %s failed to download: %s\n' \
        "$LOCAL_LLM_MODEL" "$(printf '%s' "$pull_out" | tail -1)" \
        > "$LOCAL_LLM_ERROR_FILE"
      echo "[entrypoint] ERROR: failed to pull $LOCAL_LLM_MODEL — see /api/status banner. Last log line:"
      printf '%s\n' "$pull_out" | tail -1
    fi
  fi
else
  echo "[entrypoint] LOCAL_LLM_MODEL is empty, skipping model pull"
fi

# Hand off to Pipecat. Ollama keeps running as our orphaned-by-exec
# child; when the container shuts down both processes go away
# together, which is what we want.
echo "[entrypoint] starting pipecat app"
exec /app/venv/bin/python main.py
