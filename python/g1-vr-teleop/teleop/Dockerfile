# Teleop bridge: serves the Quest WebXR page and the operator console, turns
# hand and controller poses into G1 arm and Dex3 hand targets, and streams
# them to the onboard commander. It cannot move the robot by itself: the
# onboard only obeys while an operator has armed it.
#
# micromamba, not python:slim: the arm IK does `from pinocchio import casadi`,
# and pip's `pin` wheel ships Pinocchio without the CasADi bindings. It
# imports fine and fails only at that line. conda-forge's build has them.
FROM mambaorg/micromamba:2.0-debian12-slim

USER root
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates openssl iproute2 \
    && rm -rf /var/lib/apt/lists/*

RUN micromamba install -y -n base -c conda-forge \
        python=3.12 \
        pinocchio \
        casadi \
        numpy \
        pyyaml \
        pyzmq \
        msgpack-python \
        msgpack-numpy \
    && micromamba clean --all --yes

ENV PATH=/opt/conda/bin:$PATH

# dex-retargeting maps fingers (DexPilot) and needs torch; CPU-only is enough.
# It imports `six` without declaring it, so install that explicitly.
RUN pip install --no-cache-dir aiohttp==3.10.11 six \
    && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir dex_retargeting==0.5.0

# G1 URDF + meshes and the Dex3 hand config, from Unitree's xr_teleoperate
# (Apache-2.0) at a pinned commit. Pinocchio's URDF loader needs the meshes.
ARG XR_TELEOPERATE_REF=817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b
RUN git clone --filter=blob:none --no-checkout \
        https://github.com/unitreerobotics/xr_teleoperate.git /tmp/xr \
    && git -C /tmp/xr sparse-checkout set assets/g1 assets/unitree_hand \
    && git -C /tmp/xr checkout ${XR_TELEOPERATE_REF} \
    && mkdir -p /app/assets \
    && mv /tmp/xr/assets/g1 /tmp/xr/assets/unitree_hand /app/assets/ \
    && cp /tmp/xr/LICENSE /app/assets/LICENSE \
    && rm -rf /tmp/xr

COPY bridge ./bridge
COPY web ./web
# Measured arm joint limits, so the arm solve knows what is reachable. The
# onboard keeps its own copy (onboard/calibration) and is the one that clamps.
COPY calibration ./calibration
COPY entrypoint.sh ./entrypoint.sh

# Arm, smoothing and waist settings from the Avangrid conference build.
# The waist starts locked; unlock it from the operator console.
ENV PYTHONUNBUFFERED=1 \
    TELEOP_PORT={{.HTTP_PORT}} \
    TELEOP_TLS_PORT={{.HTTPS_PORT}} \
    TELEOP_RETARGET=g1 \
    TELEOP_SINK=realg1 \
    TELEOP_ONBOARD_URL=ws://127.0.0.1:8090/ctrl \
    TELEOP_CONTROL_KEY= \
    TELEOP_RECORDING={{.RECORDING}} \
    TELEOP_SESSION_DIR=/sessions \
    TELEOP_ARM_CAL=/app/calibration/arm-limits.json \
    TELEOP_ARM=geometry \
    TELEOP_SHOULDER=1 \
    TELEOP_SHOULDER_YAW=1 \
    TELEOP_WAIST_FROZEN=1 \
    TELEOP_REALG1_WAIST_SEND_HZ=100 \
    TELEOP_SMOOTH_MODE=oneeuro \
    TELEOP_SMOOTH_MIN_CUTOFF=1.0 \
    TELEOP_SMOOTH_BETA=3.0 \
    TELEOP_SMOOTH_TAU=0.1 \
    TELEOP_SMOOTH_SHOULDER_COST=0.75 \
    TELEOP_SMOOTH_ELBOW_COST=1.5 \
    TELEOP_SMOOTH_WRIST_COST=1.0 \
    TELEOP_REALG1_SEND_HZ=90

EXPOSE {{.HTTP_PORT}} {{.HTTPS_PORT}}
# Through sh: wendy init does not keep the executable bit.
CMD ["sh", "/app/entrypoint.sh"]
