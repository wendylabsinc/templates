# go2-broadcaster — RTMP push of the robot camera to Twitch.
#
# Sibling container to go2-RC (and realsense). Runs on the Jetson with
# `network: host` so it can reach the camera upstream at
# 127.0.0.1:8000. ffmpeg is the heavy dep; everything else is tiny.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./

# Camera source. Default points at the realsense sample's color stream
# (matches go2-RC's CAMERA_UPSTREAM_URL default). For Watchtower's
# compressed video, point this at Watchtower's MJPEG endpoint.
ENV CAMERA_UPSTREAM_URL=http://127.0.0.1:8000/stream/color

# Twitch ingest. The path before the stream key is fixed; the key
# itself MUST be supplied at runtime via the environment, never baked
# into the image.
ENV TWITCH_INGEST_URL=rtmp://live.twitch.tv/app

ENV PORT=3700

EXPOSE 3700

# ENTRYPOINT (not CMD) so `wendy run --user-args=--twitch-stream-key=...`
# *appends* args to the python invocation instead of replacing it.
ENTRYPOINT ["python", "main.py"]
