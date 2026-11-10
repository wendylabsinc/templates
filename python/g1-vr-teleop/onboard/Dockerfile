# Onboard commander: the only process in this template that moves the robot.
#
# Runs ON the G1 because it needs the robot's internal DDS network. It starts
# in STANDBY and stays there until an operator presses Take control and then
# Arm on the operator console, so a deploy alone never moves an arm.
FROM python:3.12-slim AS builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential cmake \
    && rm -rf /var/lib/apt/lists/*

# CycloneDDS 0.10.x, the version unitree_sdk2_python is built against.
ARG CYCLONEDDS_REF=5041f3560c088c99e5088b2b8520b69169621196
RUN git clone --branch releases/0.10.x https://github.com/eclipse-cyclonedds/cyclonedds /tmp/cdds \
    && git -C /tmp/cdds checkout ${CYCLONEDDS_REF} \
    && cmake -S /tmp/cdds -B /tmp/cdds/build \
        -DCMAKE_INSTALL_PREFIX=/opt/cyclonedds -DBUILD_SHARED_LIBS=ON \
        -DENABLE_SSL=OFF -DENABLE_SECURITY=OFF -DBUILD_EXAMPLES=OFF \
    && cmake --build /tmp/cdds/build -j4 --target install && rm -rf /tmp/cdds
ENV CYCLONEDDS_HOME=/opt/cyclonedds

# opencv-python-headless is for the optional head camera (g1_camera.py).
RUN pip install --no-cache-dir --prefix=/install numpy==2.2.6 cyclonedds==0.10.2 aiohttp==3.10.11 \
        opencv-python-headless==4.10.0.84

# unitree_sdk2_python publishes no tags; pinned to a known commit.
ARG UNITREE_SDK2_PYTHON_REF=814556d15970dd2ecf1c9984e845ca02ab07e206
RUN git clone https://github.com/unitreerobotics/unitree_sdk2_python.git /tmp/sdk \
    && git -C /tmp/sdk checkout ${UNITREE_SDK2_PYTHON_REF} \
    && pip install --no-cache-dir --no-deps --prefix=/install /tmp/sdk \
    && cp -r /tmp/sdk/unitree_sdk2py/utils/lib \
        /install/lib/python3.12/site-packages/unitree_sdk2py/utils/lib \
    && rm -rf /tmp/sdk

FROM python:3.12-slim
# libglib for headless OpenCV; iproute2 to find the robot-network interface.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 iproute2 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /install /usr/local
COPY --from=builder /opt/cyclonedds /opt/cyclonedds
ENV LD_LIBRARY_PATH=/opt/cyclonedds/lib
WORKDIR /app
COPY *.py ./
# Measured arm joint limits. Used until you walk your own robot's limits
# (they are saved to /state/arm_cal.json); never wider than the datasheet.
COPY calibration ./calibration
COPY entrypoint.sh ./entrypoint.sh

# Settings used at the Avangrid conference build (Oct 2026). The head camera
# (RealSense colour, usually /dev/video4) only runs when recording is on.
ENV PYTHONUNBUFFERED=1 \
    G1_NET={{.NETWORK_INTERFACE}} \
    G1_DOMAIN={{.DDS_DOMAIN}} \
    G1_PORT=8090 \
    G1_KEY={{.ROBOT_KEY}} \
    G1_DEADMAN={{.DEADMAN}} \
    G1_LOCOMOTION={{.WALKING}} \
    G1_CONTROL_HZ=100 \
    G1_ARM_GAIN_PROFILE=smooth_v1 \
    G1_DEX3_KP=2.0 \
    G1_DEX3_KD=0.2 \
    G1_MOTOR_CPU=auto \
    G1_LOWSTATE_CPU=auto \
    G1_PYTHON_SWITCH_MS=0.5 \
    G1_SEND_HANDS=1 \
    G1_CAM={{.RECORDING}} \
    G1_CAMERA=4 \
    G1_STATE_DIR=/state

EXPOSE 8090
CMD ["/app/entrypoint.sh"]
