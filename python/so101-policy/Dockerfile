# syntax=docker/dockerfile:1.6
# SO-101 policy runner: LeRobot 0.6.1 runs a trained ACT or SmolVLA policy on
# the follower arm, with the same LeRobot version as the wendy train image.
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# torch first, so LeRobot's torch requirement is already met. `wendy run`
# passes WENDY_HAS_CUDA from the device's agent. An x86_64 device with an
# NVIDIA GPU gets the CUDA 12.8 build (the one the training image uses; it
# runs on older NVIDIA drivers than PyPI's default CUDA 13 build). Every other
# device gets the CPU build: the arm64 CUDA wheels target server GPUs, not
# Jetson boards, so a Jetson runs the policy on its CPU (see the README).
ARG WENDY_HAS_CUDA=false
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$WENDY_HAS_CUDA" = "true" ] && [ "$(uname -m)" = "x86_64" ]; then \
        pip install --index-url https://download.pytorch.org/whl/cu128 torch==2.11.0 torchvision==0.26.0; \
    else \
        pip install --index-url https://download.pytorch.org/whl/cpu torch==2.11.0 torchvision==0.26.0; \
    fi

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY so101_common.py policy_settings.py run_policy.py ./

# Runtime defaults; override any of them with `wendy run --env KEY=VALUE`.
# SO101_MODEL_REVISION is unset, so the model's main branch is resolved to a
# commit at startup; set it to pin a commit, branch or tag. Downloaded models
# are cached in the persistent volume at /data, so a restart needs no network
# for a revision it has loaded before. TORCH_HOME keeps the ImageNet ResNet18
# weights that torchvision downloads when LeRobot builds an ACT model (the
# checkpoint's own weights then replace them).
ENV HF_HOME=/data/huggingface \
    TORCH_HOME=/data/torch \
    SO101_MODEL_PATH=TakuyaHiraoka/act_so101_pick_diverse_objects \
    SO101_FOLLOWER_PORT=/dev/{{.FOLLOWER_SERIAL_DEVICE}} \
    SO101_FOLLOWER_CALIBRATION=/calibration/follower.json \
    SO101_CAMERAS=/dev/video0 \
    SO101_CAMERA_FOURCC=MJPG \
    SO101_FPS=30 \
    SO101_HTTP_PORT={{.PORT}}

EXPOSE {{.PORT}}

CMD ["python3", "run_policy.py"]
