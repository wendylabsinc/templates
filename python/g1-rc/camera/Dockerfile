# g1-camera — minimal MJPEG bridge for the G1's front camera.
#
# Much smaller than go2-camera: no CycloneDDS, no aiortc, no WebRTC,
# no portaudio. Just OpenCV + FastAPI. Cold build ~1 min, warm ~15 s.
FROM python:3.11-slim

ARG DEBIAN_FRONTEND=noninteractive

# libgl1 + libglib2.0-0 are OpenCV's system deps (it links libGL even
# headless for the video backend). ffmpeg + libv4l-0 cover RTSP and
# V4L2 sources respectively — G1 firmwares vary on which one's used.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libv4l-0 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./

ENV PYTHONUNBUFFERED=1
ENV PORT=8000
ENV JPEG_QUALITY=80
# Default capture source. RealSense D-series enumerates several
# /dev/video* nodes (depth, IR1, IR2, colour, motion, …). On the
# G1's D435i the colour stream is typically `/dev/video4`. If `4`
# doesn't decode, try the other indices listed by
# `wendy device camera list --device <host>`.
#
#   - `4` (typical)                   → likely RealSense colour node
#   - `0` / `1` / `2` / `3` / `5`     → other RealSense streams (depth/IR/…)
#   - `rtsp://...`                    → if the firmware ships an RTSP server
ENV CAMERA_SOURCE={{.CAMERA_SOURCE}}

EXPOSE 8000

CMD ["python", "main.py"]
