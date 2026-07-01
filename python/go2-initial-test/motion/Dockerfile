# syntax=docker/dockerfile:1.6
# go2-initial-test — motion test (SportClient walk). MANUAL trigger only.
#
# Same proven base as the go2-motion demo: Ubuntu 22.04 + CycloneDDS 0.10.5 from
# source + unitree_sdk2_python pinned.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-dev git build-essential cmake pkg-config ca-certificates ccache \
    && rm -rf /var/lib/apt/lists/*
ENV CCACHE_DIR=/ccache

ARG CYCLONEDDS_VERSION=0.10.5
RUN --mount=type=cache,target=/ccache git clone --depth 1 --branch ${CYCLONEDDS_VERSION} \
        https://github.com/eclipse-cyclonedds/cyclonedds.git /tmp/cyclonedds \
    && cmake -S /tmp/cyclonedds -B /tmp/cyclonedds/build \
        -DCMAKE_INSTALL_PREFIX=/usr/local -DBUILD_EXAMPLES=OFF -DBUILD_TESTING=OFF -DBUILD_IDLC=ON \
        -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
    && cmake --build /tmp/cyclonedds/build -j"$(nproc)" \
    && cmake --install /tmp/cyclonedds/build && ldconfig && rm -rf /tmp/cyclonedds
ENV CYCLONEDDS_HOME=/usr/local

WORKDIR /app
ARG UNITREE_SDK2_PYTHON_REF=db9b2d210081387fcd1e7ed9ac4c56a02983bb85
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip3 install --upgrade pip \
    && pip3 install -r requirements.txt \
    && pip3 install \
        "git+https://github.com/unitreerobotics/unitree_sdk2_python.git@${UNITREE_SDK2_PYTHON_REF}" \
    && SDKDIR="$(python3 -c 'import importlib.util, os; print(os.path.dirname(importlib.util.find_spec("unitree_sdk2py").origin))')" \
    && mkdir -p "$SDKDIR/b2" && touch "$SDKDIR/b2/__init__.py"
# Stub the missing b2/__init__.py (unitree_sdk2_python issues #93/#73).

COPY go2_controller.py app.py ./

ENV PORT=3616
# Robot controller IP the app routes to so it can auto-detect this host's DDS bind
# address at startup. Set GO2_DDS_ADDRESS at runtime to override the detection.
ENV GO2_IP={{.GO2_IP}}
# Skill the (hard-gated) acrobatics test runs. Override e.g. FrontJump/Handstand.
ENV ACROBATIC_SKILL=FrontFlip
EXPOSE 3616

CMD ["python3", "app.py"]
