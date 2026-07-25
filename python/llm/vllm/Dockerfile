# syntax=docker/dockerfile:1.6
#
# DFlash serving path for Laguna S 2.1 — only reachable when the picker's
# "laguna-s-2.1-dflash" option is selected, which targets NVIDIA 128GB-class
# unified-memory hardware (Jetson AGX Thor, DGX Spark). The NGC vLLM image is
# multi-arch and covers all of those, so unlike ../ollama/Dockerfile there is
# no WENDY_GPU_VENDOR / WENDY_JETPACK_MAJOR base-image matrix here.
FROM nvcr.io/nvidia/vllm:26.05-py3

# BF16 draft (1B, ~2GB) rather than the NVFP4 draft: draft memory is
# negligible next to the ~60GB target, and BF16 avoids stacking a second
# NVFP4-kernel unknown on Thor's sm_110. Swap DFLASH_DRAFT_MODEL to
# poolside/Laguna-S-2.1-DFlash-NVFP4 later as an optimization.
#
# GPU_MEMORY_UTILIZATION: vLLM's 0.9 default assumes discrete VRAM; on
# unified memory it would reserve ~110GB of Thor's 122.8GiB and starve the
# OS and Open WebUI. 0.70 holds weights + draft + KV cache with headroom.
ENV HF_HOME="/models/hf" \
    VLLM_TARGET_MODEL="poolside/Laguna-S-2.1-NVFP4" \
    DFLASH_DRAFT_MODEL="poolside/Laguna-S-2.1-DFlash" \
    SERVED_MODEL_NAME="laguna-s-2.1" \
    NUM_SPECULATIVE_TOKENS="7" \
    GPU_MEMORY_UTILIZATION="0.70"

RUN mkdir -p /models/hf

COPY entrypoint.sh /usr/local/bin/wendy-vllm-entrypoint
RUN chmod 0755 /usr/local/bin/wendy-vllm-entrypoint

EXPOSE 8000

ENTRYPOINT []
CMD ["wendy-vllm-entrypoint"]
