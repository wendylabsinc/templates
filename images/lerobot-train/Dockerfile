# The training image `wendy train` runs on a rented Nebius GPU (linux/amd64,
# NVIDIA L40S/H100/H200). CI builds and publishes it; see README.md and
# .github/workflows/lerobot-train-image.yml. To build it by hand, with this
# folder as the context:
#   docker build --platform linux/amd64 -t lerobot-train images/lerobot-train
#
# CUDA comes from the torch wheels (cu128, NVIDIA driver >= 570), so the base
# is plain Python; the NVIDIA container runtime mounts the driver at run time.
FROM python:3.12-slim-trixie

# FFmpeg's shared libraries are what torchcodec, LeRobot's default video
# decoder, loads to read dataset videos. tini forwards a preemption's SIGTERM
# to the entrypoint and reaps orphaned dataloader workers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*

# uv, pinned by version and wheel hash (PyPI, manylinux and musllinux x86_64).
RUN printf '%s\n' 'uv==0.13.5 \
      --hash=sha256:fa8d26e08e34c29c0c4e820cf3181757086d547240b4f4d31ff1d8bd810ca946 \
      --hash=sha256:d162669e3a7811c584b03690eaa11e796ad1d309aa8229d5dfe3cbcd68a90400' > /tmp/uv.txt \
    && pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore --require-hashes -r /tmp/uv.txt \
    && rm /tmp/uv.txt

WORKDIR /opt/wendy-train
COPY pyproject.toml uv.lock ./
# --locked fails the build when uv.lock is out of date with pyproject.toml.
# No uv cache: on a CI runner it would only double the ~7 GB on disk.
# The checks after it fail the build if torchcodec cannot load FFmpeg, if
# either policy cannot be imported, or if lerobot-train cannot start.
RUN UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON=/usr/local/bin/python3.12 UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 \
    uv sync --locked --no-dev --no-install-project \
    && /opt/venv/bin/python -c "from torchcodec.decoders import VideoDecoder; import lerobot.policies.act.modeling_act, lerobot.policies.smolvla.modeling_smolvla" \
    && /opt/venv/bin/lerobot-train --help > /dev/null

COPY entrypoint.py ./

# The tag and source commit go into each trained model's card. CI passes the
# commit; a local build records "unknown".
ARG IMAGE_TAG=ghcr.io/wendylabsinc/lerobot-train:lerobot-0.6.1
ARG IMAGE_REVISION=unknown
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    LEROBOT_TRAIN_IMAGE=$IMAGE_TAG \
    LEROBOT_TRAIN_IMAGE_REVISION=$IMAGE_REVISION \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    HF_DATASETS_DISABLE_PROGRESS_BARS=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility
LABEL org.opencontainers.image.source="https://github.com/wendylabsinc/templates" \
      org.opencontainers.image.description="LeRobot 0.6.1 training (ACT, SmolVLA) for wendy train" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.revision=$IMAGE_REVISION \
      sh.wendy.lerobot.version="0.6.1"

WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--", "python", "/opt/wendy-train/entrypoint.py"]
