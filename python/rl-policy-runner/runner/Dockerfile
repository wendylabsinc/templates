# runner — loads an ONNX policy and runs the on-robot control loop over Unitree DDS.
# Builds CycloneDDS + unitree_sdk2_python from source (no arm64 wheels), plus
# onnxruntime (CPU) for inference. Build for the robot's arch: linux/arm64 on the
# Go2's Orin (wendy multibuild targets the device arch).
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        python3 python3-pip git cmake build-essential \
        libssl-dev curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# CycloneDDS 0.10.5 (matches the unitree_sdk2_python binding).
ENV CYCLONEDDS_HOME=/usr/local
RUN git clone --depth 1 --branch 0.10.5 https://github.com/eclipse-cyclonedds/cyclonedds /tmp/cyclonedds \
 && cmake -S /tmp/cyclonedds -B /tmp/cyclonedds/build -DCMAKE_INSTALL_PREFIX=/usr/local -DBUILD_IDLC=ON \
 && cmake --build /tmp/cyclonedds/build --target install -j"$(nproc)" \
 && rm -rf /tmp/cyclonedds

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir cyclonedds==0.10.5 -r requirements.txt

# unitree_sdk2_python (pinned). The empty b2/ package guards an optional import;
# derive the install path instead of hardcoding a python version.
RUN git clone --depth 1 https://github.com/unitreerobotics/unitree_sdk2_python /tmp/sdk \
 && pip3 install --no-cache-dir /tmp/sdk \
 && SDK_DIR="$(python3 -c 'import unitree_sdk2py,os;print(os.path.dirname(unitree_sdk2py.__file__))')" \
 && mkdir -p "$SDK_DIR/b2" && touch "$SDK_DIR/b2/__init__.py" \
 && rm -rf /tmp/sdk

COPY controller.py app.py ./

# Baked from template variables; safety knobs overridable per-deploy.
ENV ROBOT={{.ROBOT}} \
    CONTROL_MODE={{.CONTROL_MODE}} \
    CONTROL_HZ={{.CONTROL_HZ}} \
    OBS_DIM={{.OBS_DIM}} \
    ACT_DIM={{.ACT_DIM}} \
    POLICY_URL={{.POLICY_URL}} \
    POLICY_PATH=/policy/policy.onnx \
    GO2_IP={{.GO2_IP}} \
    GO2_DDS_ADDRESS={{.GO2_DDS_ADDRESS}} \
    USE_BASE_LIN_VEL=1 \
    ACTION_SCALE=0.25 \
    KP=25.0 KD=0.5 STOP_KD=3.0 \
    JOINT_DELTA_MAX=1.2 \
    CMD_VX=0.0 CMD_VY=0.0 CMD_VYAW=0.0 \
    MAX_VX=0.5 MAX_VY=0.3 MAX_VYAW=0.5 \
    WATCHDOG_S=0.5 \
    ENABLE_POLICY=0 \
    ENABLE_LOWCMD=0 \
    PORT=3700

EXPOSE 3700
CMD ["python3", "app.py"]
