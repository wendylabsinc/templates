# Stage 1 - Build React visualizer
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2 - Pipecat runtime.
# Jetson Orin Nano with CUDA acceleration: swap the base to
# `nvcr.io/nvidia/l4t-pytorch:r36.2.0-pth2.2-py3` (or match your JetPack). The
# default debian:bookworm-slim keeps the template portable (Pi 5, x86_64).
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    gcc \
    alsa-utils \
    libasound2 \
    portaudio19-dev \
    libsndfile1 \
    ffmpeg \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Route ALSA "default" through the `plug` plugin to the PowerConf at hw:2,0.
# The PowerConf only supports 48 kHz capture/playback at the hardware level,
# so PortAudio direct-hw access fails when Silero VAD asks for 16 kHz. The
# `plug` plugin adds in-kernel rate conversion, which is what makes
# `arecord -D plughw:2,0 -r 16000` work. Keeping the override at the
# default PCM means the app can use AUDIO_INPUT_DEVICE=default and stay at
# 16 kHz everywhere. Adjust the slave card if your USB mic is on a
# different ALSA card number — `arecord -l` inside the container shows the
# enumeration.
RUN printf 'pcm.!default {\n    type plug\n    slave.pcm "hw:2,0"\n}\nctl.!default {\n    type hw\n    card 2\n}\n' > /etc/asound.conf

WORKDIR /app

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download NLTK data at build time. Piper TTS uses punkt_tab for sentence
# tokenization and tries to download it on first import, which fails inside
# a container whose DNS isn't up at boot.
ENV NLTK_DATA=/usr/local/share/nltk_data
RUN /app/venv/bin/python -c "import nltk; nltk.download('punkt_tab', download_dir='/usr/local/share/nltk_data')"

# Pre-download Piper voice (en_US-lessac-medium, ~63 MB) so the container can
# start offline and avoid a ~5s first-connection stall.
RUN mkdir -p /app/models/piper && \
    curl -fsSL -o /app/models/piper/en_US-lessac-medium.onnx \
      https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx && \
    curl -fsSL -o /app/models/piper/en_US-lessac-medium.onnx.json \
      https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json

# Pre-download faster-whisper TINY (~75 MB) into the default HF cache so the
# first WebSocket connection doesn't block on a HuggingFace round-trip.
RUN /app/venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8')"

COPY main.py pipeline.py ./
COPY --from=frontend /frontend/dist ./static

# GOOGLE_API_KEY is baked in from the template variable at scaffold time.
# If you want to rotate keys without rebuilding, move this to runtime env.
ENV GOOGLE_API_KEY={{.GOOGLE_API_KEY}}
ENV PORT={{.PORT}}
ENV PYTHONUNBUFFERED=1

# PyAudio device indexes for the local audio path. "default" means PyAudio's
# system default. After first deploy, look at the "PyAudio devices:" log line
# main.py prints on startup and override these via `wendy run --var ...`.
ENV AUDIO_INPUT_DEVICE={{.AUDIO_INPUT_DEVICE}}
ENV AUDIO_OUTPUT_DEVICE={{.AUDIO_OUTPUT_DEVICE}}

EXPOSE {{.PORT}}

CMD ["/app/venv/bin/python", "main.py"]
