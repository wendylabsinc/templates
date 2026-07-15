#!/bin/bash
# Set up the DeepStream runtime environment before launching Python.
#
# On WendyOS the DeepStream + CUDA libraries are mounted from the host via CDI
# (/etc/cdi/nvidia.yaml). We point GStreamer and the dynamic linker at them here.

export GST_PLUGIN_PATH="/usr/lib/gstreamer-1.0/deepstream"
export LD_LIBRARY_PATH="/opt/nvidia/deepstream/deepstream-7.1/lib:/usr/lib/gstreamer-1.0/deepstream:/usr/lib/aarch64-linux-gnu:/usr/lib:/usr/local/cuda-12.6/lib"
export GST_DEBUG="${GST_DEBUG:-1}"
export EGL_PLATFORM="device"
export CUDA_VER="12.6"

# Engine cache lives on the persistent volume so the slow first-run TensorRT
# build only happens once.
mkdir -p /data/engines /data/events

# Clear the GStreamer plugin cache so freshly-mounted DeepStream plugins are found.
rm -rf ~/.cache/gstreamer-1.0/ 2>/dev/null

exec /opt/venv/bin/python /app/security_camera.py
