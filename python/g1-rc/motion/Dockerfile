# syntax=docker/dockerfile:1.6
# G1 humanoid motion API. Same shape as go2-rc's motion service — Ubuntu
# 22.04 base + cyclonedds 0.10.5 from source + unitree_sdk2_python. Only
# the higher-level client differs at runtime (LocoClient vs SportClient).
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-dev \
        git \
        build-essential \
        cmake \
        pkg-config \
        ca-certificates \
        ccache \
    && rm -rf /var/lib/apt/lists/*

ENV CCACHE_DIR=/ccache

# Build CycloneDDS 0.10.5 from source. Matched to what
# unitree_sdk2_python expects (no PyPI wheels for arm64 Linux on this
# version, and the system apt repos don't ship anything compatible).
ARG CYCLONEDDS_VERSION=0.10.5
RUN --mount=type=cache,target=/ccache \
    git clone --depth 1 --branch ${CYCLONEDDS_VERSION} \
        https://github.com/eclipse-cyclonedds/cyclonedds.git /tmp/cyclonedds \
    && cmake -S /tmp/cyclonedds -B /tmp/cyclonedds/build \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DBUILD_EXAMPLES=OFF \
        -DBUILD_TESTING=OFF \
        -DBUILD_IDLC=ON \
        -DCMAKE_C_COMPILER_LAUNCHER=ccache \
        -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
    && cmake --build /tmp/cyclonedds/build -j"$(nproc)" \
    && cmake --install /tmp/cyclonedds/build \
    && ldconfig \
    && rm -rf /tmp/cyclonedds

# cyclonedds-python and unitree_sdk2_python both look up the native lib here.
ENV CYCLONEDDS_HOME=/usr/local

WORKDIR /app

# unitree_sdk2_python publishes no tags. go2-rc pins to `db9b2d2...`,
# which predates the G1 LocoClient — `unitree_sdk2py.g1` isn't in that
# tree. Use the master branch here so the G1 modules are present.
# Risk: master may break unexpectedly. Acceptable for v1; pin to a
# known-good SHA once a build is verified on your robot.
ARG UNITREE_SDK2_PYTHON_REF=master

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip3 install --upgrade pip \
    && pip3 install -r requirements.txt \
    && pip3 install \
        "git+https://github.com/unitreerobotics/unitree_sdk2_python.git@${UNITREE_SDK2_PYTHON_REF}" \
    && mkdir -p /usr/local/lib/python3.10/dist-packages/unitree_sdk2py/b2 \
    && touch /usr/local/lib/python3.10/dist-packages/unitree_sdk2py/b2/__init__.py
# Upstream ships unitree_sdk2py/b2/ without an __init__.py. setuptools
# then drops the directory from the wheel and the package's top-level
# import blows up with a misleading "circular import" error. Stub it.
# See unitreerobotics/unitree_sdk2_python issues #93 and #73.

COPY main.py g1_controller.py g1_arm.py ./

ENV PORT=3201
ENV G1_NETWORK_INTERFACE={{.NETWORK_INTERFACE}}

EXPOSE 3201

CMD ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3201"]
