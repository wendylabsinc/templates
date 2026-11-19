# syntax=docker/dockerfile:1.6
# SO-101 calibration: a small web page that reads the arm's servo positions
# while you move it by hand. It needs only the servo bus and Flask (no ROS, no
# torch, no OpenCV), so the image stays small and builds quickly.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY port_detect.py calibrate.py ./

# SO101_CALIB_PORT=auto probes the serial devices visible in the container and
# uses the one where the Feetech servos answer. Set a path such as /dev/ttyACM0
# to skip probing.
ENV SO101_CALIB_PORT=auto \
    SO101_BAUD=1000000 \
    SO101_SERVO_IDS=1,2,3,4,5,6 \
    SO101_JOINT_NAMES=shoulder_pan,shoulder_lift,elbow_flex,wrist_flex,wrist_roll,gripper \
    SO101_GRIPPER_JOINT=gripper \
    SO101_CALIB_HTTP_PORT={{.PORT}} \
    SO101_DATA_DIR=/data \
    SO101_CALIB_ID=follower

EXPOSE {{.PORT}}

CMD ["python3", "calibrate.py"]
