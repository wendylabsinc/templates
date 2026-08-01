# syntax=docker/dockerfile:1.6
#
# MAX serving path for Laguna S 2.1 — only reachable when the picker's
# "laguna-s-2.1-max" option is selected, which targets NVIDIA 128GB-class
# unified-memory hardware (Jetson AGX Thor, DGX Spark). Like ../vllm/Dockerfile
# and unlike ../ollama/Dockerfile there is no WENDY_GPU_VENDOR /
# WENDY_JETPACK_MAJOR base-image matrix: Modular publishes one multi-arch image
# (arm64 + amd64) that covers every target this pick makes sense on.
#
# Tag choice: MAX gained the Laguna architecture (LagunaForCausalLM, NVFP4 +
# bfloat16) on 2026-06-24, six days after 26.4.0 shipped, so no stable release
# can serve this model — a nightly is required until 26.5.0 is out, and the tag
# has to be a dated one so a rebuild months from now still gets this build
# rather than whatever "nightly" points at. Bump to 26.5.0 (or later stable)
# once released. Modular lists Jetson Thor as "known compatible for
# development" rather than "tested for serving" (only B200 is), and upstream
# verified Laguna itself on a B200, so treat Thor throughput as unproven until
# measured on device.
FROM modular/max-nvidia-full:26.5.0.dev2026073014

# MAX_LENGTH: Laguna's native 262144 would size the KV cache for contexts that
# don't fit next to ~72GB of weights inside the memory budget below; 131072
# keeps first boot reliable — same reasoning as ../vllm/Dockerfile.
#
# DEVICE_MEMORY_UTILIZATION: MAX's default assumes discrete VRAM; on unified
# memory that would starve the OS and Open WebUI of Thor's 122.8GiB. 0.70 holds
# weights + KV cache with headroom.
#
# QUANTIZATION_ENCODING: the poolside NVFP4 repo is compressed-tensors, which
# MAX exposes as float4_e2m1fnx2. The only other encoding MAX supports for
# Laguna is bfloat16 — ~236GB for S 2.1, so not an option on a 128GB device.
#
# MAX_SERVE_* are MAX's own settings aliases (they already default to
# 0.0.0.0:8000 / metrics on 8001); they are spelled out here because both ports
# are published in docker-compose.yml.
ENV HF_HOME="/models/hf" \
    HF_HUB_ENABLE_HF_TRANSFER="1" \
    MAX_TARGET_MODEL="poolside/Laguna-S-2.1-NVFP4" \
    SERVED_MODEL_NAME="laguna-s-2.1" \
    QUANTIZATION_ENCODING="float4_e2m1fnx2" \
    MAX_LENGTH="131072" \
    DEVICE_MEMORY_UTILIZATION="0.70" \
    MAX_SERVE_HOST="0.0.0.0" \
    MAX_SERVE_PORT="8000" \
    MAX_SERVE_METRICS_ENDPOINT_PORT="8001"

# Both are compose volumes: weights (~72GB) in the first, MAX's compiled-kernel
# cache in the second. Without the second one every restart recompiles the
# model graph, which is minutes of dead air on a cold start.
RUN mkdir -p /models/hf /opt/venv/share/max/.max_cache

COPY entrypoint.sh /usr/local/bin/wendy-max-entrypoint
RUN chmod 0755 /usr/local/bin/wendy-max-entrypoint

EXPOSE 8000 8001

# The stock image's entrypoint is `max serve` itself; override it so the
# CUDA preflight and the retry/backoff loop in entrypoint.sh run first.
ENTRYPOINT []
CMD ["wendy-max-entrypoint"]
