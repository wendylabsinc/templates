# syntax=docker/dockerfile:1.6
# Stage 1 - Build React visualizer
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
# `npm ci` for reproducible builds: it requires package-lock.json (which
# we commit) and fails if the lockfile is out of sync rather than
# silently repairing it the way `npm install` does.
RUN --mount=type=cache,target=/root/.npm npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2 - Pipecat runtime, lightweight cloud-LLM build.
#
# Base: debian:bookworm-slim. Image is ~700 MB compressed, ~1 GB
# extracted — roughly 10× lighter than the previous dustynv/tensorrt
# Jetson base. Trade-offs:
#   * No CUDA, no GPU acceleration. faster-whisper runs on CPU
#     int8 (~300-500 ms per turn on Orin's Cortex-A78AE — usable
#     for a voice loop, slower than CUDA float16 was).
#   * No on-device LLM daemon. Pick a cloud provider in the settings
#     drawer (Google / OpenAI / Anthropic / Groq). The "ollama"
#     provider still appears in the dropdown but the daemon isn't
#     bundled — selecting it surfaces "Local LLM disabled in this
#     build" in /api/status.
#   * Portable across Pi 5 / x86_64 / generic ARM64 hosts again.
# To get GPU Whisper + offline LLM back, see the
# voice-ai-pipecat-jetson template (work in progress) or the
# `cn/voice-ai-pipecat-local-llm` history before it was reverted.
FROM debian:bookworm-slim

# Bookworm ships Python 3.11 — pipecat-ai requires 3.10+. Most heavy
# Python deps install as prebuilt manylinux wheels (ctranslate2,
# faster-whisper, piper-tts, onnxruntime, numpy, scipy, …), but PyAudio
# is source-only — it ships an sdist and compiles its `_portaudio` C
# extension at install time, so we keep `gcc` in the runtime base.
# python3-dev is already required for the same reason.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        python3-dev \
        gcc \
        ccache \
        ca-certificates \
        curl \
        alsa-utils \
        libasound2 \
        portaudio19-dev \
        libsndfile1 \
        ffmpeg \
        openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
# Stage seed assets at /opt/seed-models so the persist mount at
# /models on first run can't hide them. entrypoint.sh copies them
# into /models on first boot.
ENV SEED_MODELS_DIR=/opt/seed-models
# Runtime defaults point at the persist mount. The pre-download
# steps below override these locally so the cached weights end up
# in the seed dir instead of the (empty) persist mount.
ENV HF_HOME=/models/huggingface
ENV XDG_CACHE_HOME=/models/cache

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/ccache \
    CC="ccache gcc" CXX="ccache g++" CCACHE_DIR=/ccache \
    pip install -r requirements.txt

# Pre-download NLTK data at build time. Piper TTS uses punkt_tab for sentence
# tokenization and tries to download it on first import, which fails inside
# a container whose DNS isn't up at boot.
ENV NLTK_DATA=/usr/local/share/nltk_data
RUN /app/venv/bin/python -c "import nltk; nltk.download('punkt_tab', download_dir='/usr/local/share/nltk_data')"

# Pre-download only the default Piper voice (~63 MB) so the
# settings drawer renders immediately on first connection. Other voices
# in the picker (en_US-ryan-high, en_US-amy-medium, en_GB-alan-medium)
# fetch on first selection — this is fine because PiperTTSService
# already handles missing files by downloading them at load time, and
# saves ~190 MB of image bloat that most deployments never use. Override
# the default below by changing the `tts_voice` setting in /api/settings;
# add custom voices by dropping <name>.onnx + <name>.onnx.json into
# /models/piper/ on the device. Piper hosts all voices under the same
# /resolve/main/ prefix on rhasspy/piper-voices.
RUN mkdir -p ${SEED_MODELS_DIR}/piper ${SEED_MODELS_DIR}/huggingface ${SEED_MODELS_DIR}/cache && \
    for triple in \
      "en/en_US/lessac/medium en_US-lessac-medium" ; do \
        path="$(echo $triple | cut -d' ' -f1)"; \
        name="$(echo $triple | cut -d' ' -f2)"; \
        curl -fsSL -o ${SEED_MODELS_DIR}/piper/${name}.onnx \
          https://huggingface.co/rhasspy/piper-voices/resolve/main/${path}/${name}.onnx && \
        curl -fsSL -o ${SEED_MODELS_DIR}/piper/${name}.onnx.json \
          https://huggingface.co/rhasspy/piper-voices/resolve/main/${path}/${name}.onnx.json ; \
    done

# Pre-download faster-whisper TINY (~75 MB). compute_type=int8 here
# matches the runtime path on this CPU-only base, so the pre-download
# warms the right kernels too.
RUN HF_HOME=${SEED_MODELS_DIR}/huggingface \
    XDG_CACHE_HOME=${SEED_MODELS_DIR}/cache \
    /app/venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8')"

# Pre-download openWakeWord models (~few MB each). The pretrained set
# includes "alexa", "hey_jarvis", "hey_mycroft", "hey_rhasspy", "ok_nabu".
# At runtime WAKE_WORD_MODELS picks which to load. There is no pretrained
# "Hey Wendy" — that requires training a custom model; the settings
# drawer's wake-word picker only exposes the pretrained set.
RUN HF_HOME=${SEED_MODELS_DIR}/huggingface \
    XDG_CACHE_HOME=${SEED_MODELS_DIR}/cache \
    /app/venv/bin/python -c "import openwakeword.utils as u; u.download_models()"

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

# Log per-turn STT transcripts and TTS responses to stdout. Default
# false in template.json for privacy — don't capture user speech in
# production logs. Flip to true at scaffold time with
# `wendy run --var LOG_TRANSCRIPTS=true` for debugging. Latency
# telemetry (timing, char counts) is logged regardless.
ENV LOG_TRANSCRIPTS={{.LOG_TRANSCRIPTS}}

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

# CPU-only Whisper. faster-whisper's int8 path on Cortex-A78AE / x86
# is plenty for the voice loop (~300-500 ms per turn). Override at
# runtime with WHISPER_DEVICE=cuda + WHISPER_COMPUTE_TYPE=float16 if
# you swap in a CUDA-capable base image; the runtime code respects
# both env vars without a rebuild.
ENV WHISPER_DEVICE=cpu
ENV WHISPER_COMPUTE_TYPE=int8

EXPOSE {{.PORT}}

# entrypoint.sh sets up ALSA / TLS / persist seed, then exec's the
# Python app. No on-device LLM daemon in this build — pick a cloud
# provider from the settings drawer.
CMD ["/app/entrypoint.sh"]
