# syntax=docker/dockerfile:1.6
# GPU / CUDA test for go2-initial-test.
#
# torch must match the Go2's JetPack. Default base targets JetPack 6 / L4T r36
# (Orin). For a JetPack 5 Go2 (L4T r35) override BASE_IMAGE, e.g.
#   dustynv/pytorch:2.1-r35.4.1
ARG BASE_IMAGE={{.GPU_BASE_IMAGE}}
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# The dustynv base pins PIP_INDEX_URL to the Jetson wheel index, which isn't
# resolvable from inside buildkit — with --extra-index-url pip retries it 5x per
# transitive dep before falling back to PyPI. These are pure-PyPI packages (torch/
# CUDA are already in the base), so override the index entirely with PyPI.
#
# Use `python3 -m pip` (NOT bare `pip3`): the dustynv image ships multiple Python
# interpreters, and bare pip3 can install into a different one than the `python3`
# that runs CMD below — packages land off the runtime interpreter's path and the
# container crash-loops with ModuleNotFoundError. `-m pip` guarantees we install
# into the SAME python3 that runs the app.
# uvicorn 0.33.0 (not 0.34.0): the JetPack-5 base (dustynv/pytorch:*-r35*) ships
# Python 3.8, and uvicorn>=0.34 requires Python >=3.9. 0.33.0 is the highest
# py3.8-compatible release and also works on the JetPack-6 base (py3.10), so this
# single pin builds on both. fastapi 0.115.6 supports py3.8+.
RUN --mount=type=cache,target=/root/.cache/pip python3 -m pip install --index-url https://pypi.org/simple \
        fastapi==0.115.6 uvicorn==0.33.0
# Fail the build here (not on-device at runtime) if the app's interpreter can't
# see its deps — turns a crash-loop into a loud build error. Include `torch`: if
# this line fails it reveals `python3` isn't the torch interpreter, so the whole
# install/launch chain would need to target the real torch-python instead.
RUN python3 -c "import torch, fastapi, uvicorn; print('gpu deps ok')"

# The dustynv base keeps Python in a venv at /opt/venv and exposes it via the
# image PATH. The container runtime does NOT inherit that PATH, so a bare
# `python3` at exec time resolves to the system interpreter WITHOUT uvicorn and
# the app crash-loops (even though the build guard above passed). Bake a launcher
# that execs the ABSOLUTE real path of the build interpreter (the venv python
# that has the deps), independent of whatever PATH the runtime supplies.
# NOTE: exec the real path — a symlink can break venv (pyvenv.cfg) detection.
RUN printf '#!/bin/sh\nexec "%s" "$@"\n' "$(python3 -c 'import sys; print(sys.executable)')" > /usr/local/bin/gpu-python \
 && chmod +x /usr/local/bin/gpu-python \
 && /usr/local/bin/gpu-python -c "import uvicorn, torch; print('launcher ok')"

# ── JetPack 7 / WendyOS 0.17 GPU fix (WendyOS#1370; mirrors WendyOS PR #1379) ──
# The default dustynv base targets JetPack 6 (CUDA 12.x + cuDNN 9). On a JetPack-7
# Orin the host injects CUDA 13, and the base's own default LD_LIBRARY_PATH lists
# /usr/local/cuda/compat (a bundled CUDA-12 forward-compat *driver*) first, which
# shadows the host's real CUDA-13 driver and dies at cuInit with Error 801 — so
# torch.cuda.is_available() goes False. Fix: gather the image's CUDA-12 *runtime*
# libs (toolkit under targets/*/lib + cuDNN in the multiarch dir) into one dir,
# put it first on the loader path, and drop the compat dir so the host CUDA-13
# driver is used (CUDA 12 runs on it via backward compat, and the CUDA-12 build
# ships the Orin sm_87 kernels the CUDA-13 sbsa build lacks). Never bundle
# libcuda.so* — the GPU driver must stay the CDI/host one.
RUN mkdir -p /opt/cuda12/lib \
 && find /usr/local/cuda/targets/*/lib /usr/lib/aarch64-linux-gnu -maxdepth 1 \( \
        -name 'libcudart.so*'  -o -name 'libcublas*.so*'   -o -name 'libcufft*.so*'   \
     -o -name 'libcurand.so*'  -o -name 'libcusolver*.so*' -o -name 'libcusparse.so*' \
     -o -name 'libnvrtc*.so*'  -o -name 'libnpp*.so*'      -o -name 'libcudnn*.so*'   \
   \) ! -name 'libcuda.so*' -exec ln -sf {} /opt/cuda12/lib/ \; 2>/dev/null \
 && echo /opt/cuda12/lib > /etc/ld.so.conf.d/000-cuda12.conf \
 && ldconfig
ENV LD_LIBRARY_PATH=/opt/cuda12/lib

COPY app.py ./

ENV PORT=3610
EXPOSE 3610

# Launch via the baked absolute-path interpreter, not a bare `python3`.
CMD ["/usr/local/bin/gpu-python", "app.py"]
