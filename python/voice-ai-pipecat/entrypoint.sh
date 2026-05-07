#!/bin/bash
# Container entrypoint: set up ALSA / TLS / persist seed, then hand
# off to the Pipecat app. This is the lightweight cloud-LLM build —
# no Ollama daemon, no on-device LLM. Selecting the "ollama" provider
# in /api/settings still works as a UI option but surfaces an error
# banner ("Local LLM disabled in this build") via the
# /tmp/voice-ai-local-llm-error.txt mechanism main.py polls.

set -uo pipefail

# Generate /etc/asound.conf from ALSA_CARD. Defaults target the Anker
# PowerConf the template was originally designed for, but on Pi / generic
# USB mic setups the user can set ALSA_CARD to whatever `arecord -l`
# shows in `card N: <NAME> [...]`. Setting ALSA_CARD=skip leaves the
# system asound.conf alone, which lets the PyAudio device picker drive
# routing without an asound.conf override (useful when the host already
# has a working /etc/asound.conf or when the user wants raw hw:N,M
# selection from the UI).
ALSA_CARD=${ALSA_CARD:-PowerConf}
if [ "$ALSA_CARD" = "skip" ]; then
  echo "[entrypoint] ALSA_CARD=skip — leaving /etc/asound.conf untouched"
else
  cat > /etc/asound.conf <<ASOUND_CONF
# Default PCM is split per-direction via 'asym', with each leg routed
# through its own 'plug' for in-kernel rate/channel conversion. We
# previously tried dmix/dsnoop here for shared access, but that hit a
# 'parameters->channelCount <= maxChans' PortAudio error after
# settings-driven pipeline restarts: dmix's plug-reported max channel
# count (128) doesn't roundtrip cleanly. The single-plug-per-direction
# layout is the simplest config that supports concurrent pipeline
# restarts without that failure mode.
pcm.!default {
    type asym
    playback.pcm "wendy_playback"
    capture.pcm  "wendy_capture"
}

pcm.wendy_playback {
    type plug
    slave.pcm "hw:CARD=$ALSA_CARD,DEV=0"
}

pcm.wendy_capture {
    type plug
    slave.pcm "hw:CARD=$ALSA_CARD,DEV=0"
}

ctl.!default {
    type hw
    card $ALSA_CARD
}
ASOUND_CONF
  echo "[entrypoint] /etc/asound.conf routed to ALSA card '$ALSA_CARD'"
fi

# Seed /etc/hosts at runtime. The dustynv JetPack base image ships an
# empty /etc/hosts, which causes Python httpx (used by the openai
# client → cloud LLM integration) to fail getaddrinfo for
# "localhost" with EAI_AGAIN. curl works (its own resolver shortcut),
# but the openai client doesn't, so pipecat's loopback paths are broken until
# we pin loopback resolution. We can't do this in the Dockerfile
# because BuildKit bind-mounts /etc/hosts read-only at build time.
if ! grep -q '^127\.0\.0\.1[[:space:]]\+localhost' /etc/hosts 2>/dev/null; then
  printf '127.0.0.1\tlocalhost\n::1\tlocalhost\n' > /etc/hosts || \
    echo "[entrypoint] WARN: could not seed /etc/hosts (continuing anyway)"
fi

# Ensure model subdirs exist on the /models persist volume. The
# Dockerfile creates these at build time, but wendy.json mounts a
# fresh empty persist volume at /models on first run which hides the
# image's content. Piper / faster-whisper expect their subdirs to
# exist before they try to write into them; without this,
# PiperTTSService.__init__ crashes with FileNotFoundError before
# uvicorn can finish startup.
mkdir -p /models/piper /models/huggingface /models/cache /models/tls /models/state

# Seed assets from the image layer onto the persist volume on first
# boot. The Dockerfile downloads Piper voices, Whisper weights, and
# openWakeWord models into /opt/seed-models so the persist mount can't
# hide them. Copy each subtree only if the destination is missing the
# corresponding file — re-copying every boot would just churn disk on
# Pi/Jetson without changing anything.
SEED_MODELS_DIR=${SEED_MODELS_DIR:-/opt/seed-models}
if [ -d "$SEED_MODELS_DIR" ]; then
  for sub in piper huggingface cache; do
    src="$SEED_MODELS_DIR/$sub"
    dst="/models/$sub"
    if [ -d "$src" ]; then
      mkdir -p "$dst"
      # cp -n leaves existing files alone (-n: no-clobber). Combined
      # with the per-file copy that means re-runs are cheap, and a
      # user-uploaded custom voice in /models/piper survives.
      find "$src" -mindepth 1 -maxdepth 1 -print0 \
        | xargs -0 -I{} cp -rn {} "$dst/" 2>/dev/null || true
    fi
  done
  echo "[entrypoint] seeded /models from $SEED_MODELS_DIR (first-boot only)"
fi
# HF_HOME and XDG_CACHE_HOME default to the persist mount via the
# Dockerfile's runtime ENV. No-op here unless the user overrode them,
# in which case `wendy run --var ...` already exported the override.

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
  # Pick the best hostname for the cert CN. Prefer WENDY_HOSTNAME (set
  # by the wendy agent at deploy time) because $(hostname) inside the
  # container only returns the short kernel hostname (e.g. "wendy"),
  # not the device's actual mDNS name like "wendyos-mighty-kayak". Fall
  # back to $(hostname).local. Either way ensure a single .local suffix
  # so the CN matches what the browser will resolve.
  TLS_PRIMARY_HOST="${WENDY_HOSTNAME:-$(hostname)}"
  case "$TLS_PRIMARY_HOST" in
    *.local) ;;
    *)       TLS_PRIMARY_HOST="$TLS_PRIMARY_HOST.local" ;;
  esac
  # Stuff the cert with several SANs so it matches whichever hostname
  # the developer happens to type into the browser. Order matters for
  # the CN (first one wins); the rest are alternatives.
  TLS_SHORT_HOST="$(hostname)"
  TLS_SAN="DNS:$TLS_PRIMARY_HOST,DNS:$TLS_SHORT_HOST,DNS:$TLS_SHORT_HOST.local,DNS:localhost,IP:127.0.0.1"
  if openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
       -keyout "$TLS_KEY" -out "$TLS_CERT" \
       -subj "/CN=$TLS_PRIMARY_HOST" \
       -addext "subjectAltName=$TLS_SAN" \
       2>/dev/null; then
    echo "[entrypoint] TLS cert generated for $TLS_PRIMARY_HOST (self-signed; SAN=$TLS_SAN)"
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

# Clear any stale local-LLM error from a prior deploy. main.py reads
# this file once at startup and surfaces it on /api/status as
# `local_llm_load_error`; we don't want a leftover "Ollama daemon
# died…" message from a previous Jetson build to keep showing.
#
# We deliberately don't write a "Local LLM disabled in this build"
# banner here either: main.py's _ollama_health_watcher already polls
# Ollama and only surfaces an error when the user has actively
# selected the "ollama" provider in settings. Writing an unconditional
# disabled-message would make the banner persist for users who picked
# a cloud provider, which is the wrong UX.
LOCAL_LLM_ERROR_FILE=${LOCAL_LLM_ERROR_FILE:-/tmp/voice-ai-local-llm-error.txt}
rm -f "$LOCAL_LLM_ERROR_FILE"

# Hand off to Pipecat.
echo "[entrypoint] starting pipecat app"
exec /app/venv/bin/python main.py
