# Hand cameras: the two USB cameras in the G1's hands, served to teleop on
# port 8091. Its own service so camera work never shares a process with the
# onboard commander, which is the only code that moves the arms.
FROM python:3.12-slim

# libglib for headless OpenCV.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir numpy==2.2.6 aiohttp==3.10.11 \
        opencv-python-headless==4.10.0.84

WORKDIR /app
COPY hand_cameras.py ./

# HAND_CAM_LEFT / HAND_CAM_RIGHT: "auto", or a /dev/v4l/by-path link name
# (the USB port). auto takes the non-RealSense cameras in port order; the
# console's Swap button fixes left/right and saves it in /state/cameras.json.
ENV PYTHONUNBUFFERED=1 \
    HAND_CAMERAS={{.HAND_CAMERAS}} \
    HAND_CAM_LEFT={{.LEFT_HAND_CAMERA}} \
    HAND_CAM_RIGHT={{.RIGHT_HAND_CAMERA}} \
    HAND_CAM_MATCH= \
    HAND_CAM_FPS={{.CAMERA_FPS}} \
    HAND_CAM_W=640 \
    HAND_CAM_H=480 \
    HAND_CAM_PORT=8091 \
    HAND_CAM_STATE_DIR=/state

EXPOSE 8091
CMD ["python3", "/app/hand_cameras.py"]
