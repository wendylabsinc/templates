#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$HF_HOME"

# CUDA preflight, before the ~72GB download: permanent platform mismatches
# (e.g. this image's torch built on a newer CUDA than the JetPack driver —
# see "DFlash mode" in the template README) crash at CUDA init. Without this
# check they would crash-loop in the serve loop below forever while the app's
# readiness probe (which watches the WebUI port only) keeps reporting
# healthy. A few retries cover slow GPU runtime setup; a persistent failure
# exits non-zero so a broken platform surfaces as a broken app.
cuda_ok=0
for attempt in 1 2 3; do
  if python3 -c 'import torch; torch.zeros(1, device="cuda")' >/dev/null 2>&1; then
    cuda_ok=1
    break
  fi
  echo "CUDA preflight failed (attempt ${attempt}/3); retrying in 10s..."
  sleep 10
done
if [[ "$cuda_ok" != "1" ]]; then
  echo "ERROR: cannot initialize CUDA in this container. On Jetson this usually" >&2
  echo "means the JetPack driver is older than the CUDA this image's torch was" >&2
  echo "built against (Tegra has no CUDA forward compatibility) — see 'DFlash" >&2
  echo "mode' in the template README for the JetPack requirement." >&2
  exit 1
fi

# Flags follow the model's vLLM recipe (recipes.vllm.ai/poolside/Laguna-S-2.1):
# quantization is auto-detected from the repo (never pass --quantization; the
# NVFP4 repo is compressed-tensors, not modelopt), and the poolside_v1 parsers
# make tool calls and thinking blocks come out structured instead of as raw
# XML in the chat text. The API key is the shared local constant Open WebUI is
# handed in docker-compose.yml — not a secret, but it keeps the published
# :8000 from answering unauthenticated requests.
BASE_ARGS=(
  serve "$VLLM_TARGET_MODEL"
  --host 0.0.0.0 --port 8000
  --api-key "$VLLM_API_KEY"
  --served-model-name "$SERVED_MODEL_NAME"
  --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION"
  --max-model-len "$MAX_MODEL_LEN"
  --max-num-batched-tokens 32768
  --enable-auto-tool-choice
  --tool-call-parser poolside_v1
  --reasoning-parser poolside_v1
)

# This vLLM implements Laguna natively (config and model classes), so the
# default poolside repos load with remote code execution disabled even though
# they ship an auto_map. Set TRUST_REMOTE_CODE=1 only when swapping
# VLLM_TARGET_MODEL to a repo this build has no native implementation for —
# it lets that repo run arbitrary code in a GPU-entitled container.
if [[ "${TRUST_REMOTE_CODE:-0}" == "1" ]]; then
  BASE_ARGS+=(--trust-remote-code)
fi

# DFlash needs a vLLM with upstream support (vllm-project/vllm#46853). Probe
# for the drafter's model class rather than trusting the container tag or a
# version number (NGC builds carry backports and forked version strings), and
# fall back to plain (non-speculative) serving so the app works either way.
# The probe runs inside the condition, so a broken python/vLLM install
# demotes to the plain path instead of tripping set -e.
DFLASH_ARGS=()
if [[ "${DFLASH_DISABLE:-0}" != "1" ]] \
  && python3 -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("vllm.model_executor.models.laguna_dflash") else 1)' >/dev/null 2>&1; then
  echo "vLLM build supports DFlash; enabling speculative decoding with ${DFLASH_DRAFT_MODEL}."
  DFLASH_ARGS=(
    --speculative-config "{\"method\": \"dflash\", \"model\": \"${DFLASH_DRAFT_MODEL}\", \"num_speculative_tokens\": ${NUM_SPECULATIVE_TOKENS}}"
    # The recipe pairs DFlash with the triton MoE backend.
    --moe-backend triton
  )
else
  echo "WARNING: this vLLM build has no DFlash support (or DFLASH_DISABLE=1); serving ${VLLM_TARGET_MODEL} without speculative decoding."
fi

echo "First start downloads ~72GB of weights into ${HF_HOME}; the OpenAI API on :8000 stays down until the model finishes loading."

# Keep retrying like ../ollama/entrypoint.sh: first boot often races network
# readiness, and a transiently-down network should mean visible retry logs
# rather than a dead container. Two escape hatches keep permanent breakage
# from looping forever: an attempt WITH DFlash enabled that dies quickly is
# blamed on the speculative config and demoted to plain serving, and repeated
# quick deaths with nothing left to demote exit non-zero — the compose
# restart policy then restarts the container with backoff, so the failure
# shows up as a restarting container instead of a healthy-looking loop.
# bash stays PID 1 (no exec) so the loop survives vLLM exits; docker stop
# falls back to the grace-period kill.
FAST_EXIT_SECONDS=90
MAX_CONSECUTIVE_FAST_EXITS=5
fast_exits=0
while true; do
  start=$SECONDS
  if vllm "${BASE_ARGS[@]}" ${DFLASH_ARGS[0]:+"${DFLASH_ARGS[@]}"}; then
    exit 0
  fi
  if (( SECONDS - start < FAST_EXIT_SECONDS )); then
    if (( ${#DFLASH_ARGS[@]} > 0 )); then
      echo "WARNING: vLLM exited quickly with DFlash enabled; retrying WITHOUT speculative decoding."
      DFLASH_ARGS=()
      fast_exits=0
    else
      fast_exits=$(( fast_exits + 1 ))
      if (( fast_exits >= MAX_CONSECUTIVE_FAST_EXITS )); then
        echo "ERROR: vLLM died within ${FAST_EXIT_SECONDS}s of starting ${fast_exits} times in a row; treating this as permanent, not transient." >&2
        exit 1
      fi
    fi
  else
    fast_exits=0
  fi
  echo "vLLM exited; retrying in 15s..."
  sleep 15
done
