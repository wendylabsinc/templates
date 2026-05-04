ARG JETSON_BASE_IMAGE=dustynv/tensorrt:8.6-r36.2.0

# Stage 1 - Build React visualizer
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2 - Pipecat runtime on Jetson with CUDA + cuDNN + on-device LLM.
#
# Default base: dustynv/tensorrt:8.6-r36.2.0 — public Docker Hub Jetson
# image with L4T r36, CUDA 12.2, cuDNN 8.9, TensorRT, and Python on
# Ubuntu 22.04. This avoids requiring an NGC docker login during buildx
# pulls while preserving the CUDA/cuDNN stack CTranslate2 needs.
# CTranslate2 is pinned below to v4.4.0, the CUDA 12 + cuDNN 8 compatible
# line. CTranslate2 v4.5.0+ moved to cuDNN 9.
#
# We need a base with cuDNN, not just bare CUDA, because CTranslate2's
# CUDA backend — which faster-whisper rides on — links against cuDNN.
FROM ${JETSON_BASE_IMAGE}

# JetPack 6 ships Ubuntu 22.04 with Python 3.10 native — no deadsnakes
# PPA needed (the previous r35/Ubuntu-20.04 base did, and the launchpad
# infrastructure for that turned out to be flaky enough to repeatedly
# break the build).
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.10 \
        python3.10-venv \
        python3.10-dev \
        ca-certificates \
        curl \
        gnupg \
        zstd \
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

# Ollama for on-device LLM serving. We deliberately bypass Ollama's
# upstream install.sh: on Jetson r36.x images it can silently abort (the
# script's Jetson-detection branch fetches a JetPack-tagged bundle URL
# its env-probe builds, and that path is fragile inside slim L4T
# images). Downloading the tarballs directly is deterministic and
# avoids the install script entirely.
#
# Ollama's distribution layout (as of recent releases): the generic
# arm64 tarball ships the binary + base runners, and the JetPack 6
# tarball is an additive bundle that drops CUDA-tuned runners on top.
# install.sh installs both in turn — we mirror that. Format is
# .tar.zst, hence zstd in the apt step above.
RUN set -eu; \
    BASE_URL="https://ollama.com/download"; \
    curl -fsSL --retry 5 --retry-delay 3 --max-time 300 \
        -o /tmp/ollama.tar.zst \
        "$BASE_URL/ollama-linux-arm64.tar.zst"; \
    tar --zstd -C /usr/local -xf /tmp/ollama.tar.zst; \
    rm /tmp/ollama.tar.zst; \
    if curl -fsSL --retry 3 --retry-delay 3 --max-time 300 \
            -o /tmp/ollama-jp6.tar.zst \
            "$BASE_URL/ollama-linux-arm64-jetpack6.tar.zst"; then \
      tar --zstd -C /usr/local -xf /tmp/ollama-jp6.tar.zst; \
      rm /tmp/ollama-jp6.tar.zst; \
      echo "Installed Ollama with JetPack 6 add-on"; \
    else \
      echo "JetPack 6 add-on unavailable, using generic Ollama runners only"; \
    fi; \
    /usr/local/bin/ollama --version

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
# Put nvcc / cuda libs on PATH and LD_LIBRARY_PATH for the CTranslate2
# source build. Jetson CUDA images ship CUDA at /usr/local/cuda but
# does NOT add it to PATH by default, which causes cmake's CUDA
# detection to fail silently and fall back to a CPU-only build (the
# smoke test catches this — but only after a 9-min compile).
ENV CUDA_HOME=/usr/local/cuda
ENV PATH="/app/venv/bin:${CUDA_HOME}/bin:${PATH}"
ENV LD_LIBRARY_PATH="/usr/local/lib:${CUDA_HOME}/lib64:${LD_LIBRARY_PATH:-}"
ENV CTRANSLATE2_ROOT=/usr/local
ENV HF_HOME=/models/huggingface
ENV XDG_CACHE_HOME=/models/cache
ENV OLLAMA_MODELS=/models/ollama

# ctranslate2 from source. The generic aarch64 wheel does not reliably
# target Jetson's L4T CUDA/cuDNN stack or Orin's SM architecture, so build
# it in-image against the CUDA 12.2 + cuDNN 8.9 libraries that the base
# provides. Pin to v4.4.0: v4.5.0+ requires cuDNN 9, but JetPack 6.0 GA
# ships cuDNN 8.9. faster-whisper 1.x accepts ctranslate2 4.x, and
# requirements.txt pins this exact version so pip does not replace it.
#
# This step takes 10–15 min on first build; subsequent rebuilds hit
# the layer cache so it's a one-time cost.
# CUDA_ARCH_LIST is the SM (compute capability) the kernels are
# compiled for. Default 8.7 = Jetson Orin (Nano/NX/AGX). For Xavier
# (sm_72), TX2 (sm_62), or non-Jetson CUDA builds, override at build
# time: `--build-arg CTRANSLATE2_CUDA_ARCH="7.2;7.5;8.6;8.7"`. Listing
# multiple arches makes the image portable at the cost of a longer
# build and a larger wheel.
ARG CTRANSLATE2_CUDA_ARCH=8.7
# Cap parallelism at half the available cores to keep nvcc memory
# usage in check on smaller Jetsons (Orin Nano 4 GB OOMs at full
# nproc on the CUDA kernel compile).
RUN set -eu; \
    test -x "${CUDA_HOME}/bin/nvcc" \
      || (echo "ERROR: nvcc not found at ${CUDA_HOME}/bin/nvcc; the Jetson base image is missing the CUDA toolkit." \
          && ls -la /usr/local/ && exit 1); \
    cd /tmp; \
    git clone --depth 1 --branch v4.4.0 --recursive \
      https://github.com/OpenNMT/CTranslate2.git; \
    mkdir -p /tmp/CTranslate2/build; \
    cd /tmp/CTranslate2/build; \
    cmake -DCMAKE_INSTALL_PREFIX=/usr/local \
          -DCMAKE_BUILD_TYPE=Release \
          -DWITH_CUDA=ON -DWITH_CUDNN=ON \
          -DWITH_MKL=OFF -DOPENMP_RUNTIME=COMP \
          -DCMAKE_CUDA_COMPILER="${CUDA_HOME}/bin/nvcc" \
          -DCUDAToolkit_ROOT="${CUDA_HOME}" \
          -DCUDA_NVCC_FLAGS="-Xfatbin=-compress-all" \
          -DCUDA_ARCH_LIST="${CTRANSLATE2_CUDA_ARCH}" \
          .. > /tmp/cmake.log 2>&1 || (cat /tmp/cmake.log && exit 1); \
    cat /tmp/cmake.log; \
    if grep -qE "Could NOT find (CUDA|CUDNN|cuDNN)|cuDNN (include directory|libraries) not found" /tmp/cmake.log; then \
      echo "ERROR: cmake failed to find CUDA or cuDNN — see log above"; \
      exit 1; \
    fi; \
    make -j"$(( $(nproc) / 2 + 1 ))" install; \
    ldconfig; \
    cd /tmp/CTranslate2/python; \
    /app/venv/bin/pip install --no-cache-dir -r install_requirements.txt; \
    /app/venv/bin/python setup.py bdist_wheel; \
    /app/venv/bin/pip install --no-cache-dir dist/ctranslate2*.whl; \
    rm -rf /tmp/CTranslate2 /tmp/cmake.log

# Smoke-test the ctranslate2 install. Without this, a partial install
# where the Python wheel landed but the native .so failed to link CUDA
# succeeds at build time and fails at the first transcription with an
# opaque "OSError: libctranslate2.so.3: cannot open shared object file"
# (or worse, a clean import that can't actually use CUDA). CTranslate2
# 4.4.0 does not expose a compile-time boolean on the Python module; asking
# for CUDA compute types reaches the compile-time CUDA guard before any
# runtime GPU requirement.
# A BuildKit host without a GPU may still fail the runtime CUDA probe, so
# only reject the explicit "not compiled with CUDA support" error.
RUN /app/venv/bin/python - <<'PY'
import ctranslate2

try:
    ctranslate2.get_supported_compute_types("cuda")
except Exception as exc:
    message = str(exc)
    if "not compiled with CUDA support" in message:
        raise AssertionError(
            "ctranslate2 built without CUDA support — check Dockerfile "
            "WITH_CUDA / WITH_CUDNN flags and that the Jetson base ships "
            "matching CUDA + cuDNN headers"
        ) from exc
    print(f"ctranslate2 CUDA support present; runtime CUDA probe skipped: {message}")
else:
    print("ctranslate2 CUDA support present")
PY

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
RUN mkdir -p /models/piper /models/huggingface /models/cache /models/ollama && \
    for triple in \
      "en/en_US/lessac/medium en_US-lessac-medium" \
      "en/en_US/ryan/high en_US-ryan-high" \
      "en/en_US/amy/medium en_US-amy-medium" \
      "en/en_GB/alan/medium en_GB-alan-medium" ; do \
        path="$(echo $triple | cut -d' ' -f1)"; \
        name="$(echo $triple | cut -d' ' -f2)"; \
        curl -fsSL -o /models/piper/${name}.onnx \
          https://huggingface.co/rhasspy/piper-voices/resolve/main/${path}/${name}.onnx && \
        curl -fsSL -o /models/piper/${name}.onnx.json \
          https://huggingface.co/rhasspy/piper-voices/resolve/main/${path}/${name}.onnx.json ; \
    done

# Pre-download faster-whisper TINY (~75 MB) into HF_HOME so the first
# WebSocket connection doesn't block on a HuggingFace round-trip.
# compute_type=float32 here (instead of the runtime int8/float16) only
# because cross-building arm64 under qemu doesn't have a working int8
# dispatch backend (CTranslate2 is built with WITH_MKL=OFF and no Ruy
# fallback in the qemu environment). Caching the weights is the only
# side effect we need from this step. At runtime on Jetson,
# WHISPER_DEVICE=cuda + WHISPER_COMPUTE_TYPE below selects the actual
# compute path.
RUN /app/venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='float32')"

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

# Whisper compute target. The default Jetson base ships CUDA 12.2 + cuDNN
# 8.9 and the ctranslate2 step above is built with WITH_CUDA=ON, so
# cuda+float16 is the right pick on Jetson. If libcuda is missing at
# run time, override with WHISPER_DEVICE=cpu / WHISPER_COMPUTE_TYPE=int8.
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
