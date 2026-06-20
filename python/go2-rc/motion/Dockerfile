# Minimal Go2 motion API. Same shape as templates/python/go2-basic — Ubuntu
# 22.04 base + cyclonedds 0.10.5 from source + unitree_sdk2_python pinned to
# a known-good SHA. Matches what unitree_sdk2_python expects at the C-lib
# level so the SportClient bindings link cleanly on linux/arm64.
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
    && rm -rf /var/lib/apt/lists/*

# Build CycloneDDS 0.10.5 from source. Matched to what
# unitree_sdk2_python expects (no PyPI wheels for arm64 Linux on this
# version, and the system apt repos don't ship anything compatible).
ARG CYCLONEDDS_VERSION=0.10.5
RUN git clone --depth 1 --branch ${CYCLONEDDS_VERSION} \
        https://github.com/eclipse-cyclonedds/cyclonedds.git /tmp/cyclonedds \
    && cmake -S /tmp/cyclonedds -B /tmp/cyclonedds/build \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DBUILD_EXAMPLES=OFF \
        -DBUILD_TESTING=OFF \
        -DBUILD_IDLC=ON \
    && cmake --build /tmp/cyclonedds/build -j"$(nproc)" \
    && cmake --install /tmp/cyclonedds/build \
    && ldconfig \
    && rm -rf /tmp/cyclonedds

# cyclonedds-python and unitree_sdk2_python both look up the native lib here.
ENV CYCLONEDDS_HOME=/usr/local

WORKDIR /app

# unitree_sdk2_python publishes no tags; pin a known-good master SHA.
ARG UNITREE_SDK2_PYTHON_REF=db9b2d210081387fcd1e7ed9ac4c56a02983bb85

COPY requirements.txt .
RUN pip3 install --no-cache-dir --upgrade pip \
    && pip3 install --no-cache-dir -r requirements.txt \
    && pip3 install --no-cache-dir \
        "git+https://github.com/unitreerobotics/unitree_sdk2_python.git@${UNITREE_SDK2_PYTHON_REF}" \
    && mkdir -p /usr/local/lib/python3.10/dist-packages/unitree_sdk2py/b2 \
    && touch /usr/local/lib/python3.10/dist-packages/unitree_sdk2py/b2/__init__.py
# Upstream ships unitree_sdk2py/b2/ without an __init__.py. setuptools
# then drops the directory from the wheel and the package's top-level
# import blows up with a misleading "circular import" error. Stub it.
# See unitreerobotics/unitree_sdk2_python issues #93 and #73.

COPY main.py go2_controller.py ./

ENV PORT=3201
ENV GO2_DDS_ADDRESS={{.GO2_DDS_ADDRESS}}

EXPOSE 3201

CMD ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3201"]
