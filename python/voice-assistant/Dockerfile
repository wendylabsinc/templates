FROM ubuntu:22.04

# Install system dependencies and native libraries
# Using apt for system libraries, pip for Python packages to avoid conflicts
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    python3-dev \
    libopenblas-dev \
    libblas-dev \
    liblapack-dev \
    gfortran \
    cmake \
    build-essential \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install system dependencies
RUN apt-get update && apt-get install -y \
    # Audio libraries
    libportaudio2 \
    libasound2-dev \
    portaudio19-dev \
    alsa-utils \
    alsa-tools \
    # Required for building audio packages
    gcc \
    g++ \
    python3-dev \
    # FFmpeg for audio processing (required by Whisper)
    ffmpeg \
    # Python
    python3 \
    python3-pip \
    libsndfile1 \
    git \
    wget \
    curl \
    # TTS fallback (optional - in case piper-tts fails)
    espeak-ng \
    && rm -rf /var/lib/apt/lists/*

# Install PyTorch from NVIDIA's Jetson-optimized wheels FIRST
# These are pre-compiled for ARM64 and optimized for Jetson with CUDA 12.6
# This leverages CDI-mapped CUDA/cuDNN from the host system
# Pin specific versions to ensure we get the CUDA-enabled builds
# NOTE: Jetson AI Lab PyTorch may not depend on numpy or vendors it internally
RUN pip3 install --no-cache-dir \
    torch==2.8.0 \
    torchvision==0.23.0 \
    torchaudio==2.8.0 \
    --index-url https://pypi.jetson-ai-lab.io/jp6/cu126/

# Install ONNX Runtime GPU for Jetson (for openWakeWord acceleration)
# Uninstall any existing onnxruntime first to avoid conflicts
RUN pip3 uninstall -y onnxruntime onnxruntime-gpu || true
RUN pip3 install --no-cache-dir \
    onnxruntime-gpu==1.23.0 \
    --extra-index-url https://pypi.jetson-ai-lab.io/jp6/cu126/

# Install core Python packages (excluding piper-tts which can be problematic)
RUN pip3 install --no-cache-dir \
    openwakeword>=0.5.0 \
    openai-whisper>=20231117 \
    numpy>=1.24.0 \
    sounddevice>=0.4.6 \
    scipy>=1.11.0 \
    transformers>=4.35.0 \
    pydub>=0.25.1 \
    requests>=2.31.0

# Install piper-tts WITHOUT dependencies to avoid conflict with onnxruntime-gpu
# Using --no-deps ensures piper uses our already-installed onnxruntime-gpu
# This enables GPU acceleration for both openWakeWord AND Piper TTS!
RUN pip3 install --no-cache-dir --no-deps piper-tts>=1.2.0 || \
    echo "⚠ piper-tts installation failed - will use fallback TTS"

# Install scientific Python packages with pinned numpy version
RUN pip3 install --no-cache-dir \
    numpy==1.26.4 \
    scipy \
    matplotlib

# Verify we got the correct PyTorch version (will fail build if wrong)
# Use pip show instead of import to avoid CUDA library loading during build
RUN pip3 show torch | grep "Version: 2.8.0" && echo "✓ Correct PyTorch version installed"

# Set working directory and copy application code
WORKDIR /app
COPY wendy_assistant.py .

# Set environment variables
ENV PYTHONUNBUFFERED=1
# Configure ALSA for proper audio device detection
ENV ALSA_CARD=0
ENV ALSA_DEVICE=0
ENV PYTHONIOENCODING=utf-8
ENV ORT_LOGGING_LEVEL=3
ENV KMP_DUPLICATE_LIB_OK=TRUE

# Default command - run bash to keep container alive
CMD ["python3", "wendy_assistant.py"]