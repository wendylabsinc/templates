# Pick a Jetson-compatible CUDA/PyTorch base.
# For JetPack 6 / L4T 36.x, this is a common starting point.
FROM dustynv/l4t-pytorch:r36.4.0

WORKDIR /app

# System deps:
# - libsndfile1 + ffmpeg are explicitly recommended for NeMo installs
# - alsa-utils gives us arecord for mic capture
RUN apt-get update && apt-get install -y \
    libsndfile1 ffmpeg \
    alsa-utils \
    git curl \
    libnuma-dev \
 && rm -rf /var/lib/apt/lists/*

# Override base image pip config - use env vars so subprocesses inherit them
ENV PIP_INDEX_URL=https://pypi.org/simple/
ENV PIP_EXTRA_INDEX_URL=https://pypi.ngc.nvidia.com

# Remove any pre-existing pip.conf that may have unreachable indexes
RUN rm -f /etc/pip.conf ~/.config/pip/pip.conf ~/.pip/pip.conf 2>/dev/null || true

# Ensure setuptools is up to date
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Cache bust: v2
# NeMo dependencies and NeMo itself
# Use NeMo v2.0.0 which is compatible with torch 2.4.0 (Jetson base image version)
# Install with --no-deps to avoid replacing Jetson-compiled torch/torchvision
# Pin packages to versions compatible with L4T's glibc (Ubuntu 22.04 = glibc 2.35)
RUN echo "cache-bust-v8" && pip install --no-cache-dir Cython packaging \
 && pip install --no-cache-dir --no-deps "nemo_toolkit[asr] @ git+https://github.com/NVIDIA/NeMo.git@v2.0.0" \
 && pip install --no-cache-dir \
    omegaconf hydra-core "pytorch-lightning==2.2.0" webdataset \
    braceexpand editdistance einops g2p_en inflect jiwer \
    lhotse "librosa<0.10" marshmallow matplotlib more-itertools nltk \
    pandas pydub ruamel.yaml sentencepiece soundfile sox text-unidecode \
    "transformers<4.38" tqdm wrapt wget "huggingface_hub<0.21" python-dateutil \
    numba llvmlite pyannote.audio datasets \
 && pip install --no-cache-dir "fastapi" "uvicorn[standard]" "scipy<1.12"

# Fix libnuma glibc compatibility - remove any incompatible versions and reinstall system one
RUN rm -f /usr/lib/libnuma* /usr/local/lib/libnuma* 2>/dev/null || true \
 && apt-get update && apt-get install --reinstall -y libnuma1 libnuma-dev \
 && rm -rf /var/lib/apt/lists/* \
 && ldconfig

# Grab the cache-aware streaming pipeline config referenced by the model card (optional but nice)
RUN curl -L \
  -o /app/cache_aware_rnnt.yaml \
  https://raw.githubusercontent.com/NVIDIA-NeMo/NeMo/main/examples/asr/conf/asr_streaming_inference/cache_aware_rnnt.yaml

COPY app.py /app/app.py
COPY index.html /app/index.html

ENV PYTHONUNBUFFERED=1

# Set cache directories to use the persistent volume at /root/.cache
# TORCH_HOME controls torch.hub cache (for Silero VAD)
# HF_HOME controls HuggingFace cache (for Nemotron ASR)
ENV TORCH_HOME=/root/.cache/torch
ENV HF_HOME=/root/.cache/huggingface

# Port {{.PORT}}
EXPOSE {{.PORT}}

CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "{{.PORT}}"]
