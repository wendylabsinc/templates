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
RUN python3 -m pip install --no-cache-dir --index-url https://pypi.org/simple \
        fastapi==0.115.6 uvicorn==0.34.0
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
COPY app.py ./

ENV PORT=3610
EXPOSE 3610

# Launch via the baked absolute-path interpreter, not a bare `python3`.
CMD ["/usr/local/bin/gpu-python", "app.py"]
