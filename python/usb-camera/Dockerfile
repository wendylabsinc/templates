# USB/UVC RGB webcam → HTTP MJPEG. Multi-arch (arm64 ✓ for Jetson).
# opencv-python-headless ships prebuilt aarch64 wheels, so no compiler needed;
# libglib2.0-0 / libgl1 are its only runtime shared-lib deps, v4l-utils is handy
# for debugging the camera on-device (v4l2-ctl).
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
    PORT={{.PORT}} \
    VIDEO_DEVICE={{.VIDEO_DEVICE}} \
    WIDTH={{.WIDTH}} \
    HEIGHT={{.HEIGHT}} \
    FPS={{.FPS}} \
    JPEG_QUALITY={{.JPEG_QUALITY}}

EXPOSE {{.PORT}}
CMD ["python", "app.py"]
