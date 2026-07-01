# syntax=docker/dockerfile:1.6
# go2-map-zones — offline occupancy-map zone segmenter.
#
# Pure NumPy + OpenCV + FastAPI, so this is a lean slim image (~400 MB): no
# CycloneDDS, no torch, no ROS. Builds in ~1-2 min on first run, ~20s cached.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# opencv-python-headless still needs libGL + glib at runtime even though it
# draws nothing to a screen; ca-certificates so pip can fetch wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY main.py segmenter.py ./
COPY static ./static

ENV PORT={{.PORT}}
# Segmentation outputs land here. On WendyOS this is a mounted volume so the
# zones survive restarts; main.py falls back to /app/out if it isn't writable.
ENV OUTPUT_DIR=/data/{{.APP_ID}}/segmentations

EXPOSE {{.PORT}}

CMD ["python", "main.py"]
