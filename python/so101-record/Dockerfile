# syntax=docker/dockerfile:1.6
# SO-101 recorder: LeRobot 0.6.1 records leader/follower teleoperation and a
# USB camera as a LeRobotDataset. Recording needs no GPU, so the image uses
# CPU-only torch on every device (arm64 and x86_64).
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

COPY so101_common.py record_settings.py record.py ./

# Runtime defaults; override any of them with `wendy run --env KEY=VALUE`.
# The dataset is written to $HF_LEROBOT_HOME/<SO101_DATASET_REPO_ID>, inside
# the persistent volume mounted at /data. The calibration files come from the
# so101-calibration app's volume, mounted at /calibration (see wendy.json).
ENV HF_LEROBOT_HOME=/data/lerobot \
    SO101_FOLLOWER_PORT=/dev/{{.FOLLOWER_SERIAL_DEVICE}} \
    SO101_LEADER_PORT=/dev/{{.LEADER_SERIAL_DEVICE}} \
    SO101_FOLLOWER_CALIBRATION=/calibration/follower.json \
    SO101_LEADER_CALIBRATION=/calibration/leader.json \
    SO101_CAMERAS=front=/dev/video0 \
    SO101_CAMERA_WIDTH=640 \
    SO101_CAMERA_HEIGHT=480 \
    SO101_CAMERA_FOURCC=MJPG \
    SO101_FPS=30 \
    SO101_NUM_EPISODES=50 \
    SO101_EPISODE_SECONDS=30 \
    SO101_RESET_SECONDS=10 \
    SO101_DATASET_REPO_ID=local/so101_demos \
    SO101_HTTP_PORT={{.PORT}}

EXPOSE {{.PORT}}

CMD ["python3", "record.py"]
