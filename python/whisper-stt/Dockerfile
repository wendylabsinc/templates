# Speech-to-Text Docker Image for NVIDIA Jetson
# Optimized for Jetson Orin with JetPack 6.x and CUDA 12.6
FROM ubuntu:22.04

LABEL maintainer="Wendy Labs Inc"
LABEL description="Speech-to-Text using Whisper for NVIDIA Jetson with USB microphone support"

# Prevent interactive prompts during build
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Python
    python3 \
    python3-pip \
    python3-dev \
    # Audio libraries for USB microphone support
    libportaudio2 \
    portaudio19-dev \
    libasound2-dev \
    alsa-utils \
    alsa-tools \
    pulseaudio \
    # Build dependencies
    build-essential \
    gcc \
    g++ \
    cmake \
    # FFmpeg for audio processing (required by Whisper)
    ffmpeg \
    # Math libraries
    libopenblas-dev \
    libblas-dev \
    liblapack-dev \
    libsndfile1 \
    # Utilities
    git \
    wget \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install PyTorch from NVIDIA's Jetson-optimized wheels
# Pre-compiled for ARM64 with CUDA 12.6 support
RUN pip3 install --no-cache-dir \
    torch==2.8.0 \
    torchaudio==2.8.0 \
    --index-url https://pypi.jetson-ai-lab.io/jp6/cu126/

# Install core Python packages
RUN pip3 install --no-cache-dir \
    openai-whisper>=20231117 \
    numpy>=1.24.0 \
    sounddevice>=0.4.6 \
    scipy>=1.11.0

# Pin numpy version for compatibility
RUN pip3 install --no-cache-dir numpy==1.26.4

# Verify PyTorch installation
RUN pip3 show torch | grep "Version: 2.8.0" && echo "PyTorch installed successfully"

# Set working directory
WORKDIR /app

# Copy application code
COPY speech_to_text.py .

# Create directory for transcription output
RUN mkdir -p /app/output

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8

# ALSA configuration for USB microphone
ENV ALSA_CARD=0
ENV ALSA_DEVICE=0

# Whisper model configuration (can be overridden at runtime)
ENV WHISPER_MODEL=base
ENV WHISPER_LANGUAGE=en

# Default command - run in continuous mode
CMD ["python3", "speech_to_text.py", "--continuous"]
