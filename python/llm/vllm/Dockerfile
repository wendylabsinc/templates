# syntax=docker/dockerfile:1.6
#
# DFlash serving path for Laguna S 2.1 — only reachable when the picker's
# "laguna-s-2.1-dflash" option is selected, which targets NVIDIA 128GB-class
# unified-memory hardware (Jetson AGX Thor, DGX Spark). The NGC vLLM image is
# multi-arch and covers all of those, so unlike ../ollama/Dockerfile there is
# no WENDY_GPU_VENDOR / WENDY_JETPACK_MAJOR base-image matrix here.
#
# Tag choice is constrained from two sides: the vLLM needs native
# LagunaForCausalLM support (landed upstream in 0.21; 26.05's 0.20.1 cannot
# load Laguna at all), while Jetson/Spark hosts need the container's CUDA to
# match the JetPack driver (26.06/26.07 ship torch built on CUDA 13.3, which
# segfaults in cuStreamCreateWithPriority on JetPack 7.2 / CUDA 13.2 — Tegra
# has no CUDA forward compatibility). As of 2026-07 no NGC tag satisfies
# both on JetPack 7.2; 26.06 is pinned as the earliest Laguna-capable tag
# and starts working the moment the device side moves to a CUDA 13.3
# JetPack. DFlash's Laguna drafter additionally needs vLLM >= 0.25.1
# (NGC 26.08+); until then the entrypoint's probe/demotion serves Laguna on
# plain vLLM.
FROM nvcr.io/nvidia/vllm:26.06-py3

# BF16 draft (1B, ~2GB) rather than the NVFP4 draft: draft memory is
# negligible next to the ~72GB target, and BF16 avoids stacking a second
# NVFP4-kernel unknown on Thor's sm_110. Swap DFLASH_DRAFT_MODEL to
# poolside/Laguna-S-2.1-DFlash-NVFP4 later as an optimization.
#
# GPU_MEMORY_UTILIZATION: vLLM's 0.9 default assumes discrete VRAM; on
# unified memory it would reserve ~110GB of Thor's 122.8GiB and starve the
# OS and Open WebUI. 0.70 holds weights + draft + KV cache with headroom.
#
# MAX_MODEL_LEN: Laguna's native 262144 would size the KV cache for contexts
# that don't fit next to 72GB of weights in the 0.70 budget; 131072 keeps
# first boot reliable. NUM_SPECULATIVE_TOKENS=15 per the vLLM recipe for
# this model (recipes.vllm.ai/poolside/Laguna-S-2.1).
ENV HF_HOME="/models/hf" \
    VLLM_TARGET_MODEL="poolside/Laguna-S-2.1-NVFP4" \
    DFLASH_DRAFT_MODEL="poolside/Laguna-S-2.1-DFlash" \
    SERVED_MODEL_NAME="laguna-s-2.1" \
    NUM_SPECULATIVE_TOKENS="15" \
    MAX_MODEL_LEN="131072" \
    GPU_MEMORY_UTILIZATION="0.70"

RUN mkdir -p /models/hf

COPY entrypoint.sh /usr/local/bin/wendy-vllm-entrypoint
RUN chmod 0755 /usr/local/bin/wendy-vllm-entrypoint

EXPOSE 8000

ENTRYPOINT []
CMD ["wendy-vllm-entrypoint"]
