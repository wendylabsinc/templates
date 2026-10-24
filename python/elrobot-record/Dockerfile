# syntax=docker/dockerfile:1.6
# ElRobot recorder: LeRobot 0.6.1 records leader/follower teleoperation of an
# ElRobot arm and two USB cameras as a LeRobotDataset. Recording needs no GPU,
# so the image uses CPU-only torch on every device (arm64 and x86_64).
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# CPU-only torch first, so LeRobot's torch requirement is already met: on
# arm64 the default PyPI wheel is the CUDA build and adds gigabytes the
# recorder never uses.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url https://download.pytorch.org/whl/cpu \
    torch==2.11.0 torchvision==0.26.0

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# elrobot_arms.py is vendored Apache-2.0 code; its license and provenance ship with it.
COPY licenses/ ./licenses/
COPY UPSTREAM.md elrobot_arms.py elrobot_common.py record_settings.py record.py ./

# Runtime defaults; override any of them with `wendy run --env KEY=VALUE`.
# The dataset is written to $HF_LEROBOT_HOME/<ELROBOT_DATASET_REPO_ID>, inside
# the persistent volume mounted at /data. The calibration files live in the
# shared calibration volume, mounted at /calibration (see wendy.json).
# ELROBOT_CAMERAS is a guess: check the dry run's pictures (see the README).
ENV HF_LEROBOT_HOME=/data/lerobot \
    ELROBOT_FOLLOWER_PORT=/dev/{{.FOLLOWER_SERIAL_DEVICE}} \
    ELROBOT_LEADER_PORT=/dev/{{.LEADER_SERIAL_DEVICE}} \
    ELROBOT_FOLLOWER_CALIBRATION=/calibration/follower.json \
    ELROBOT_LEADER_CALIBRATION=/calibration/leader.json \
    ELROBOT_CAMERAS=arm=/dev/video0,table=/dev/video2 \
    ELROBOT_CAMERA_WIDTH=640 \
    ELROBOT_CAMERA_HEIGHT=480 \
    ELROBOT_CAMERA_FOURCC=MJPG \
    ELROBOT_FPS=30 \
    ELROBOT_NUM_EPISODES=50 \
    ELROBOT_EPISODE_SECONDS=30 \
    ELROBOT_RESET_SECONDS=10 \
    ELROBOT_DATASET_REPO_ID=local/elrobot_demos \
    ELROBOT_HTTP_PORT={{.PORT}}

EXPOSE {{.PORT}}

CMD ["python3", "record.py"]
