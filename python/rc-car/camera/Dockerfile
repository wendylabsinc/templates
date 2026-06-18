# rc-car camera — generic UVC camera -> MJPEG via OpenCV/V4L2.
#
# Runs with `network: host` and the `camera` entitlement (exposes /dev/video*).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

# OpenCV headless still needs a couple of system libs for video/image codecs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./

ENV PORT=8000
ENV CAMERA_DEVICE=/dev/video0
ENV CAMERA_WIDTH=640
ENV CAMERA_HEIGHT=480
ENV CAMERA_FPS=20
ENV JPEG_QUALITY=80

EXPOSE 8000

CMD ["python", "app.py"]
