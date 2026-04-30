# Stage 1 - Build React visualizer
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2 - Pipecat runtime on Jetson with CUDA + on-device LLM.
#
# Base: nvcr.io/nvidia/l4t-base:r35.4.1 — Ubuntu 20.04 + CUDA 11.4
# userland staged for the Wendy `gpu` entitlement to bind-mount libcuda
# from the host (Go2's JetPack 5.1.x). If the dog turns out to be
# JetPack 5.1.1 (R35.3.x), swap this tag to r35.3.1 — the userland is
# CUDA 11.4 either way and the host driver is what gates the kernel
# module compat.
#
# Trade-off vs the previous debian:bookworm-slim base: the image grows
# from ~700 MB to ~3.5 GB but unlocks GPU acceleration for Ollama
# (and, optionally, faster-whisper). Pi 5 / x86_64 deploys are no
# longer in scope on this branch — the local-LLM template is
# Jetson-targeted.
FROM nvcr.io/nvidia/l4t-base:r35.4.1

# Ubuntu 20.04 default Python is 3.8; pipecat-ai requires 3.10+.
# Pull Python 3.10 from the deadsnakes PPA (officially-built backports,
# ARM64 supported). Doing this in two apt-update passes is needed
# because add-apt-repository depends on software-properties-common.
RUN apt-get update && apt-get install -y --no-install-recommends \
    software-properties-common \
    ca-certificates \
    curl \
    gnupg \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
    python3.10 \
    python3.10-venv \
    python3.10-dev \
    gcc \
    alsa-utils \
    libasound2 \
    portaudio19-dev \
    libsndfile1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Ollama for on-device LLM serving. Detects CUDA at runtime through
# the bind-mounted libcuda; no GPU at runtime → falls back to CPU
# (slow but still functional). The official installer also tries to
# create a systemd service which fails inside a container — that's
# expected and harmless; the binary install itself still succeeds, so
# we tolerate a non-zero exit and verify the binary landed.
RUN curl -fsSL https://ollama.com/install.sh | sh || true
RUN test -x /usr/local/bin/ollama || (echo "ERROR: Ollama install failed; binary missing at /usr/local/bin/ollama" && exit 1)

# Route ALSA's "default" PCM to the PowerConf via `asym` (split playback
# and capture) with `plug` on each leg for in-kernel rate conversion. We
# address the device by USB card *name* so the same setup works regardless
# of slot order — the PowerConf can land on hw:0,0 or hw:2,0 depending on
# what else is plugged in. The asound.conf heredoc below documents WHY
# this layout was picked over dmix/dsnoop. To target a different USB mic,
# change `CARD=PowerConf` to whatever `arecord -l` shows in
# `card N: <NAME> [...]`.
COPY <<'ASOUND_CONF' /etc/asound.conf
# Default PCM is split per-direction via `asym`, with each leg routed
# through its own `plug` for in-kernel rate/channel conversion. We
# previously tried dmix/dsnoop here for shared access, but that hit a
# `parameters->channelCount <= maxChans` PortAudio error after
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
    slave.pcm "hw:CARD=PowerConf,DEV=0"
}

pcm.wendy_capture {
    type plug
    slave.pcm "hw:CARD=PowerConf,DEV=0"
}

ctl.!default {
    type hw
    card PowerConf
}
ASOUND_CONF

WORKDIR /app

RUN python3.10 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download NLTK data at build time. Piper TTS uses punkt_tab for sentence
# tokenization and tries to download it on first import, which fails inside
# a container whose DNS isn't up at boot.
ENV NLTK_DATA=/usr/local/share/nltk_data
RUN /app/venv/bin/python -c "import nltk; nltk.download('punkt_tab', download_dir='/usr/local/share/nltk_data')"

# Pre-download a curated set of Piper voices (~63-100 MB each) so the
# settings drawer's voice picker doesn't stall on first selection. Add
# more by appending `<lang>/<region>/<speaker>/<quality>/<file>` triples
# below — Piper hosts them all at the same /resolve/main/ prefix.
RUN mkdir -p /app/models/piper && \
    for triple in \
      "en/en_US/lessac/medium en_US-lessac-medium" \
      "en/en_US/ryan/high en_US-ryan-high" \
      "en/en_US/amy/medium en_US-amy-medium" \
      "en/en_GB/alan/medium en_GB-alan-medium" ; do \
        path="$(echo $triple | cut -d' ' -f1)"; \
        name="$(echo $triple | cut -d' ' -f2)"; \
        curl -fsSL -o /app/models/piper/${name}.onnx \
          https://huggingface.co/rhasspy/piper-voices/resolve/main/${path}/${name}.onnx && \
        curl -fsSL -o /app/models/piper/${name}.onnx.json \
          https://huggingface.co/rhasspy/piper-voices/resolve/main/${path}/${name}.onnx.json ; \
    done

# Pre-download faster-whisper TINY (~75 MB) into the default HF cache so the
# first WebSocket connection doesn't block on a HuggingFace round-trip.
RUN /app/venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8')"

# Pre-download openWakeWord models (~few MB each). The pretrained set
# includes "alexa", "hey_jarvis", "hey_mycroft", "hey_rhasspy", "ok_nabu".
# At runtime WAKE_WORD_MODELS picks which to load. There is no pretrained
# "Hey Wendy" — that requires training a custom model; the settings
# drawer's wake-word picker only exposes the pretrained set.
RUN /app/venv/bin/python -c "import openwakeword.utils as u; u.download_models()"

COPY main.py pipeline.py entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh
COPY --from=frontend /frontend/dist ./static

# GOOGLE_API_KEY is baked in from the template variable at scaffold time.
# If you want to rotate keys without rebuilding, move this to runtime env.
ENV GOOGLE_API_KEY={{.GOOGLE_API_KEY}}
ENV PORT={{.PORT}}
ENV PYTHONUNBUFFERED=1
# Pipecat 0.0.108 emits a chorus of DeprecationWarnings about
# OpenAILLMContext / GoogleUserContextAggregator / vad_analyzer on
# every pipeline build. They're not actionable for template users
# and drown the actual logs. Filter them at interpreter start —
# warnings.filterwarnings() in main.py runs too late to catch the
# `<string>:4:` ones generated from exec'd code.
ENV PYTHONWARNINGS=ignore::DeprecationWarning

# PyAudio device indexes for the local audio path. "default" means PyAudio's
# system default. After first deploy, look at the "PyAudio devices:" log line
# main.py prints on startup and override these via `wendy run --var ...`.
ENV AUDIO_INPUT_DEVICE={{.AUDIO_INPUT_DEVICE}}
ENV AUDIO_OUTPUT_DEVICE={{.AUDIO_OUTPUT_DEVICE}}

# Wake word activation. The bot stays silent until one of these openWakeWord
# pretrained phrases fires: "alexa", "hey_jarvis", "hey_mycroft",
# "hey_rhasspy", "ok_nabu". Comma-separated for multi-model. Custom
# wake words like "hey wendy" require training a model — see
# https://github.com/dscripka/openWakeWord for the recipe.
ENV WAKE_WORD_MODELS={{.WAKE_WORD_MODELS}}
ENV WAKE_WORD_THRESHOLD={{.WAKE_WORD_THRESHOLD}}
ENV WAKE_LISTEN_SECS={{.WAKE_LISTEN_SECS}}

# On-device LLM model that the entrypoint pulls into Ollama on first
# boot (and skips on subsequent boots if already cached). Empty
# disables the pull step — the container still starts the Ollama
# daemon so the user can pull a model later via the UI's settings,
# but no LLM is available locally until they do. See entrypoint.sh.
ENV LOCAL_LLM_MODEL={{.LOCAL_LLM_MODEL}}

EXPOSE {{.PORT}}

# entrypoint.sh starts Ollama in the background, optionally pulls
# LOCAL_LLM_MODEL, then exec's the Python app. Pipecat itself doesn't
# need to know about Ollama — it talks to it as just another OpenAI-
# compatible endpoint via the runtime settings store.
CMD ["/app/entrypoint.sh"]
