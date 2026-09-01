# syntax=docker/dockerfile:1.6
#
# MAX serving layer — vendored from mojo/llm/max-serve, serving the voice
# assistant's LLM leg on 9012 (9011 stays free for a co-deployed mojo/llm
# group on the same host network). One image for every target: MAX ships
# aarch64 + x86_64 wheels and selects CPU or GPU at runtime, so no
# dustynv/JetPack base-image matrix is needed — the CUDA driver is
# injected by the host via the gpu entitlement.
#
# Hard-won Jetson notes (docs/mojo-max-port-findings.md):
#   - gcc is a RUNTIME requirement: max serve JIT-compiles Mojo modules on
#     first import and uses the C compiler as linker (MMF-017).
#   - MODULAR_DEVICE_CONTEXT_MEMORY_MANAGER_VMM=0 and
#     --no-device-graph-capture avoid the iGPU VMM graph-capture crash
#     (MMF-004 / upstream #6961) on Orin and Thor.
#   - The serving auto-tuner double-counts unified memory; explicit
#     batch/length/memory flags are mandatory on Jetson (MMF-006).
#     The utilization fraction applies to FREE memory at boot: 0.6 (not
#     mojo/llm's 0.3) so ~1 GB-class models fit beside the voice app on
#     an 8 GB Orin. Measured limits there (MMF-006 addendum): a 1.7 GB
#     CUDA neighbor made even a 256 MiB model fail its memory plan, and
#     Qwen2.5-1.5B (2.88 GiB bf16) does not plan at 0.6×free with the
#     voice app resident — hence the 0.5B default; 1.5B+ needs a 16 GB+
#     device (AGX Orin / Thor).
#   - XDG_CACHE_HOME must live on the persist volume: graph compile is
#     ~400 s cold vs ~19 s warm on an Orin Nano (MMF-018).
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    iproute2 \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir modular==26.5.0

# NOTE: like other templates in this repo, scaffold-time secrets are baked
# into the image (same caveat as voice-ai-pipecat's GOOGLE_API_KEY).
ENV HF_HOME=/models/hf \
    XDG_CACHE_HOME=/models/cache \
    MODULAR_DEVICE_CONTEXT_MEMORY_MANAGER_VMM=0 \
    HF_TOKEN="{{.HF_TOKEN}}" \
    MAX_MODEL="{{.MAX_MODEL}}" \
    MAX_LENGTH=2048 \
    MAX_BATCH_SIZE=1 \
    MAX_DEVICE_MEMORY_UTILIZATION=0.6 \
    MAX_PORT=9012

RUN mkdir -p /models

COPY entrypoint.sh /usr/local/bin/wendy-max-entrypoint
RUN chmod 0755 /usr/local/bin/wendy-max-entrypoint

EXPOSE 9012

ENTRYPOINT []
CMD ["wendy-max-entrypoint"]
