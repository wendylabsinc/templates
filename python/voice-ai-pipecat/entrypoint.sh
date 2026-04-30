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

set -u

OLLAMA_LOG=/tmp/ollama.log
OLLAMA_HOST=${OLLAMA_HOST:-http://localhost:11434}

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
# registry. We don't fail the whole container on pull failure — the
# user can still hit cloud LLMs via /api/settings, and a transient
# network issue at boot shouldn't bring the bot down entirely.
if [ -n "${LOCAL_LLM_MODEL:-}" ]; then
  if ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$LOCAL_LLM_MODEL"; then
    echo "[entrypoint] $LOCAL_LLM_MODEL already cached, skipping pull"
  else
    echo "[entrypoint] pulling $LOCAL_LLM_MODEL — first boot, expect 1–3 min on WiFi"
    if ! ollama pull "$LOCAL_LLM_MODEL"; then
      echo "[entrypoint] WARN: failed to pull $LOCAL_LLM_MODEL. Local LLM will be unavailable until next restart with internet."
    else
      echo "[entrypoint] $LOCAL_LLM_MODEL pulled successfully"
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
