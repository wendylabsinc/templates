# syntax=docker/dockerfile:1.6
# rc — teleop UI for the G1.
#
# Sibling container to the motion and camera services. Runs on the
# G1's onboard computer with `network: host` so the proxy can reach
# motion at 127.0.0.1:3201 and the camera upstream at 127.0.0.1:8000.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY main.py ./
COPY web/index.html web/wendy.css web/wendy-logo.svg ./web/

# Where g1-motion lives. Default assumes sibling container with host
# networking. Override at runtime if you point g1-RC at a remote dog.
ENV G1_MOTION_URL=http://127.0.0.1:3201

# MJPEG source proxied at /api/camera. Default points at g1-camera's
# color stream. Override for an external source (RTSP, gstreamer pipe,
# etc.); set to empty to disable the camera tile.
ENV CAMERA_UPSTREAM_URL=http://127.0.0.1:8000/stream/color

ENV PORT={{.RC_PORT}}

EXPOSE {{.RC_PORT}}

CMD ["python", "main.py"]
