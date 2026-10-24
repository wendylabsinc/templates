# syntax=docker/dockerfile:1.6
# ElRobot policy runner: LeRobot 0.6.1 runs a trained ACT or SmolVLA policy on
# the ElRobot follower arm, with the same LeRobot version as the wendy train
# image.
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

# elrobot_arms.py is vendored Apache-2.0 code; its license and provenance ship with it.
COPY licenses/ ./licenses/
COPY UPSTREAM.md elrobot_arms.py elrobot_common.py policy_settings.py run_policy.py ./

# Runtime defaults; override any of them with `wendy run --env KEY=VALUE`.
# There is no public ElRobot model, so ELROBOT_MODEL_PATH has no default: set
# it to the model you trained. ELROBOT_MODEL_REVISION unset resolves the
# model's main branch to a commit at startup. Downloaded models are cached in
# the persistent volume at /data, so a restart needs no network for a revision
# it has loaded before. TORCH_HOME keeps the ImageNet ResNet18 weights that
# torchvision downloads when LeRobot builds an ACT model (the checkpoint's own
# weights then replace them). ELROBOT_CAMERAS is a guess: check the dry run's
# pictures (see the README).
ENV HF_HOME=/data/huggingface \
    TORCH_HOME=/data/torch \
    ELROBOT_FOLLOWER_PORT=/dev/{{.FOLLOWER_SERIAL_DEVICE}} \
    ELROBOT_FOLLOWER_CALIBRATION=/calibration/follower.json \
    ELROBOT_CAMERAS=arm=/dev/video0,table=/dev/video2 \
    ELROBOT_CAMERA_FOURCC=MJPG \
    ELROBOT_FPS=30 \
    ELROBOT_HTTP_PORT={{.PORT}}

EXPOSE {{.PORT}}

CMD ["python3", "run_policy.py"]
