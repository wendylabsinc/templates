ARG ISAAC_SIM_IMAGE={{.ISAAC_SIM_IMAGE}}
FROM ${ISAAC_SIM_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive
ENV ACCEPT_EULA=Y
ENV OMNI_KIT_ACCEPT_EULA=yes
ENV OMNI_KIT_ALLOW_ROOT=1
ENV HEADLESS=1
ENV LIVESTREAM=0
ENV ENABLE_CAMERAS=0
ENV XR=0
ENV TERM=xterm
ENV PYTHONUNBUFFERED=1
ENV ISAACLAB_ROOT=/workspace/IsaacLab

WORKDIR /workspace
USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    procps \
    python-is-python3 \
    && rm -rf /var/lib/apt/lists/*

# IsaacLab's dependency stack may pull setuptools releases that are too new for
# some transitive packages. Keep the constraint local to this image.
RUN printf 'setuptools<81\n' > /tmp/pip-constraints.txt
ENV PIP_CONSTRAINT=/tmp/pip-constraints.txt

ARG ISAACLAB_REPO=https://github.com/isaac-sim/IsaacLab.git
ARG ISAACLAB_REF={{.ISAACLAB_REF}}
RUN git clone --depth 1 --branch "${ISAACLAB_REF}" "${ISAACLAB_REPO}" "${ISAACLAB_ROOT}" \
    && cd "${ISAACLAB_ROOT}" \
    && if [ -d /isaac-sim ] && [ ! -e _isaac_sim ]; then ln -s /isaac-sim _isaac_sim; fi \
    && ./isaaclab.sh --install rsl_rl

WORKDIR /workspace/app

ENV WENDY_ROBOT_PROFILE="{{.ROBOT_PROFILE}}"
ENV WENDY_TRAIN_TASK="{{.TASK_ID}}"
ENV WENDY_EXPERIMENT_NAME="{{.EXPERIMENT_NAME}}"
ENV WENDY_NUM_ENVS="{{.NUM_ENVS}}"
ENV WENDY_MAX_ITERATIONS="{{.MAX_ITERATIONS}}"
ENV WENDY_LOG_ROOT=/logs
ENV WENDY_HEADLESS=1
ENV WENDY_RECORD_VIDEO=1
ENV WENDY_VIDEO_LENGTH=300
ENV WENDY_VIDEO_INTERVAL=
ENV WENDY_AUTO_RESUME=1

COPY train.py /workspace/app/train.py
COPY scripts /workspace/app/scripts
COPY source /workspace/app/source
COPY README.md /workspace/app/README.md

ENV PYTHONPATH=/workspace/app/source/local_isaac_tasks:/workspace/app/source

ENTRYPOINT ["python3", "train.py"]
CMD []
