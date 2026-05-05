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
# cached. The model isn't baked into the image (smaller image, faster
# rebuilds) but the first boot needs the device to have an internet
# route so the pull succeeds.

set -uo pipefail

# Seed /etc/hosts at runtime. The dustynv JetPack base image ships an
# empty /etc/hosts, which causes Python httpx (used by the openai
# client → local Ollama integration) to fail getaddrinfo for
# "localhost" with EAI_AGAIN. curl works (its own resolver shortcut),
# but the openai client doesn't, so pipecat → Ollama is broken until
# we pin loopback resolution. We can't do this in the Dockerfile
# because BuildKit bind-mounts /etc/hosts read-only at build time.
if ! grep -q '^127\.0\.0\.1[[:space:]]\+localhost' /etc/hosts 2>/dev/null; then
  printf '127.0.0.1\tlocalhost\n::1\tlocalhost\n' > /etc/hosts || \
    echo "[entrypoint] WARN: could not seed /etc/hosts (continuing anyway)"
fi

# Ensure model subdirs exist on the /models persist volume. The
# Dockerfile creates these at build time, but wendy.json mounts a
# fresh empty persist volume at /models on first run which hides the
# image's content. Piper / faster-whisper / Ollama all expect their
# subdirs to exist before they try to write into them; without this,
# PiperTTSService.__init__ crashes with FileNotFoundError before
# uvicorn can finish startup.
mkdir -p /models/piper /models/huggingface /models/cache /models/ollama /models/tls

# TLS for the FastAPI/uvicorn server. Browsers gate the mic permissions
# API (navigator.mediaDevices.getUserMedia) behind a secure origin —
# HTTPS or `localhost`. Plain HTTP on the device's mDNS hostname is
# treated as insecure and the React app crashes with
# "undefined is not an object (evaluating 'navigator.mediaDevices.
# getUserMedia')". Avoid forcing every user to ssh-tunnel to localhost
# by serving HTTPS directly with a self-signed cert generated on first
# boot. Cert lives on the /models persist volume so it survives
# container restarts (and so the browser only prompts for the
# self-signed-cert exception once per developer machine).
#
# Upgrade path for zero browser warnings (and Safari support, which is
# strict about self-signed even after click-through): generate a cert
# with mkcert on your dev machine and drop the resulting
# `cert.pem` / `key.pem` into /models/tls/ before the container starts
# (e.g. via `wendy device file push`). The check below skips the
# self-signed regen whenever those files already exist.
TLS_DIR=/models/tls
TLS_CERT="$TLS_DIR/cert.pem"
TLS_KEY="$TLS_DIR/key.pem"
if [ ! -s "$TLS_CERT" ] || [ ! -s "$TLS_KEY" ]; then
  echo "[entrypoint] generating self-signed TLS cert in $TLS_DIR"
  TLS_HOST="$(hostname).local"
  if openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
       -keyout "$TLS_KEY" -out "$TLS_CERT" \
       -subj "/CN=$TLS_HOST" \
       -addext "subjectAltName=DNS:$TLS_HOST,DNS:localhost,IP:127.0.0.1" \
       2>/dev/null; then
    echo "[entrypoint] TLS cert generated for $TLS_HOST (self-signed)"
  else
    echo "[entrypoint] WARN: openssl cert generation failed; falling back to plain HTTP"
    rm -f "$TLS_CERT" "$TLS_KEY"
  fi
else
  echo "[entrypoint] TLS cert already present in $TLS_DIR (using existing)"
fi
if [ -s "$TLS_CERT" ] && [ -s "$TLS_KEY" ]; then
  export TLS_CERT_FILE="$TLS_CERT"
  export TLS_KEY_FILE="$TLS_KEY"
fi

OLLAMA_LOG=/tmp/ollama.log
OLLAMA_HOST=${OLLAMA_HOST:-http://localhost:11434}
# main.py reads this on startup and surfaces it on /api/status as
# `local_llm_load_error`, so a failed model pull shows in the UI as a
# banner instead of silently breaking every LLM turn at runtime.
LOCAL_LLM_ERROR_FILE=${LOCAL_LLM_ERROR_FILE:-/tmp/voice-ai-local-llm-error.txt}
rm -f "$LOCAL_LLM_ERROR_FILE"

echo "[entrypoint] starting ollama daemon (logs → $OLLAMA_LOG)"
# Bind on 0.0.0.0 because the GPU entitlement's network namespace
# routes inbound traffic from the Pipecat side via the bridge
# interface, not the loopback — a 127.0.0.1 bind is unreachable
# from the same container under that setup. On host networking
# this binds Ollama to every interface; trust the LAN or front it
# with a firewall.
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
