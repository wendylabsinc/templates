# go2-RC — teleop UI for the Go2.
#
# Sibling container to go2-motion (and optionally realsense). Runs on
# the Jetson with `network: host` so the proxy can reach go2-motion at
# 127.0.0.1:3201 and the camera upstream at 127.0.0.1:8000.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY static ./static

# Where go2-motion lives. Default assumes sibling container with host
# networking. Override at runtime if you point go2-RC at a remote dog.
ENV GO2_MOTION_URL=http://127.0.0.1:3201

# MJPEG source proxied at /api/camera. Default points at the realsense
# sample's color stream. For Watchtower's compressed video, point this
# at whatever URL Watchtower exposes; for "no camera", set to empty.
ENV CAMERA_UPSTREAM_URL=http://127.0.0.1:8000/stream/color

ENV PORT=3500

EXPOSE 3500

CMD ["python", "main.py"]
