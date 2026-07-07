# Edge component — USB/UVC RGB webcam → HTTP MJPEG. Multi-arch (arm64 ✓ for Jetson).
# Deployed to every device in the "{{.CAMERA_GROUP}}" group. opencv-python-headless
# ships prebuilt aarch64 wheels (no compiler); libglib2.0-0 / libgl1 are its only
# runtime shared-lib deps; v4l-utils is handy for debugging the camera on-device.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgl1 \
        v4l-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV PYTHONUNBUFFERED=1 \
    PORT={{.CAMERA_PORT}} \
    VIDEO_DEVICE={{.VIDEO_DEVICE}} \
    WIDTH={{.WIDTH}} \
    HEIGHT={{.HEIGHT}} \
    FPS={{.FPS}} \
    JPEG_QUALITY={{.JPEG_QUALITY}}

EXPOSE {{.CAMERA_PORT}}
CMD ["python", "app.py"]
