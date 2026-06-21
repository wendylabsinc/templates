# runner — runs the Walk-These-Ways Go2 policy (two TorchScript nets) over Unitree
# DDS. Builds CycloneDDS + unitree_sdk2_python from source, installs torch (CPU) for
# inference, and bakes in the WTW pre-trained Go2 checkpoints. Build for the robot's
# arch: linux/arm64 on the Go2 Orin (wendy multibuild targets the device arch).
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        python3 python3-pip git cmake build-essential \
        libssl-dev curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV CYCLONEDDS_HOME=/usr/local
RUN git clone --depth 1 --branch 0.10.5 https://github.com/eclipse-cyclonedds/cyclonedds /tmp/cyclonedds \
 && cmake -S /tmp/cyclonedds -B /tmp/cyclonedds/build -DCMAKE_INSTALL_PREFIX=/usr/local -DBUILD_IDLC=ON \
 && cmake --build /tmp/cyclonedds/build --target install -j"$(nproc)" \
 && rm -rf /tmp/cyclonedds

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir cyclonedds==0.10.5 -r requirements.txt

RUN git clone --depth 1 https://github.com/unitreerobotics/unitree_sdk2_python /tmp/sdk \
 && pip3 install --no-cache-dir /tmp/sdk \
 && SDK_DIR="$(python3 -c 'import unitree_sdk2py,os;print(os.path.dirname(unitree_sdk2py.__file__))')" \
 && mkdir -p "$SDK_DIR/b2" && touch "$SDK_DIR/b2/__init__.py" \
 && rm -rf /tmp/sdk

# WTW pre-trained Go2 checkpoints (MIT, Teddy-Liao/walk-these-ways-go2).
ARG WTW_BASE=https://raw.githubusercontent.com/Teddy-Liao/walk-these-ways-go2/main/runs/gait-conditioned-agility/pretrain-go2/train/142238.667503/checkpoints
RUN mkdir -p /policy \
 && curl -fsSL "$WTW_BASE/adaptation_module_latest.jit" -o /policy/adaptation_module_latest.jit \
 && curl -fsSL "$WTW_BASE/body_latest.jit" -o /policy/body_latest.jit

COPY controller.py app.py ./

ENV POLICY_DIR=/policy \
    NUM_OBS=70 HISTORY_LEN=15 \
    GO2_IP={{.GO2_IP}} \
    GO2_DDS_ADDRESS={{.GO2_DDS_ADDRESS}} \
    CMD_VX={{.CMD_VX}} CMD_VY=0.0 CMD_VYAW=0.0 \
    STEP_FREQ=3.0 FOOTSWING=0.08 \
    KP=25.0 KD=0.6 STOP_KD=3.0 \
    CONTROL_HZ=50 \
    ENABLE_POLICY=0 \
    ENABLE_LOWCMD=0 \
    PORT=3700

EXPOSE 3700
CMD ["python3", "app.py"]
