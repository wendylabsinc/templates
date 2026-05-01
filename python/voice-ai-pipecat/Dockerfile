# Stage 1 - Build React visualizer
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2 - Pipecat runtime on Jetson with CUDA + cuDNN + on-device LLM.
#
# Base: nvcr.io/nvidia/l4t-jetpack:r35.4.1 — Ubuntu 20.04 + the full
# JetPack 5.1.2 stack (CUDA 11.4, cuDNN 8.x, TensorRT, headers). We
# need cuDNN here for ctranslate2's CUDA backend (which faster-whisper
# rides on). The leaner l4t-base image only ships CUDA stubs — Ollama
# works on it because it brings its own GPU runtime, but Whisper would
# stay stuck on CPU. If the dog turns out to be JetPack 5.1.1 (R35.3.x),
# swap to r35.3.1 — the userland is CUDA 11.4 either way and the host
# driver is what gates kernel-module compat.
#
# Trade-off vs the previous l4t-base: image grows ~3.5 GB → ~6 GB but
# unlocks GPU Whisper alongside GPU Ollama. Pi 5 / x86_64 deploys are
# no longer in scope on this branch.
FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1

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
    g++ \
    cmake \
    git \
    pkg-config \
    libopenblas-dev \
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

# ctranslate2 from source — the prebuilt aarch64 wheels on PyPI are
# CPU-only (NVIDIA Jetson isn't a manylinux target). To get GPU
# Whisper we have to build ctranslate2 against the JetPack CUDA +
# cuDNN that l4t-jetpack provides. Pinned to v3.24.0: the last 3.x
# release, which targets CUDA 11. ctranslate2 4.x requires CUDA 12+
# and is incompatible with JetPack 5.1.x. faster-whisper 1.x in turn
# requires ctranslate2 4.x, so requirements.txt pins faster-whisper
# below 1.0 to match.
#
# This step takes 10–15 min on first build; subsequent rebuilds hit
# the layer cache so it's a one-time cost.
RUN cd /tmp && \
    git clone --depth 1 --branch v3.24.0 --recursive \
      https://github.com/OpenNMT/CTranslate2.git && \
    mkdir -p /tmp/CTranslate2/build && cd /tmp/CTranslate2/build && \
    cmake -DCMAKE_INSTALL_PREFIX=/usr/local \
          -DCMAKE_BUILD_TYPE=Release \
          -DWITH_CUDA=ON -DWITH_CUDNN=ON \
          -DWITH_MKL=OFF -DOPENMP_RUNTIME=COMP \
          -DCUDA_NVCC_FLAGS="-Xfatbin=-compress-all" \
          -DCMAKE_CUDA_ARCHITECTURES="87" \
          .. && \
    make -j"$(nproc)" install && \
    ldconfig && \
    cd /tmp/CTranslate2/python && \
    /app/venv/bin/pip install --no-cache-dir -r install_requirements.txt && \
    /app/venv/bin/python setup.py bdist_wheel && \
    /app/venv/bin/pip install --no-cache-dir dist/ctranslate2*.whl && \
    rm -rf /tmp/CTranslate2

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

# Whisper compute target. l4t-jetpack ships CUDA 11.4 + cuDNN 8.x and
# the ctranslate2 step above is built with WITH_CUDA=ON, so cuda+
# float16 is the right pick on Jetson — gets STT down from ~1.5 s
# (CPU tiny + int8) to ~200–300 ms (CUDA tiny + float16) on Orin NX.
# Override at run time if you're forced back to CPU (libcuda missing
# at start, etc.) by setting WHISPER_DEVICE=cpu and
# WHISPER_COMPUTE_TYPE=int8 — pipeline._build_stt_service reads both.
ENV WHISPER_DEVICE=cuda
ENV WHISPER_COMPUTE_TYPE=float16

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
