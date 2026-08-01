#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$HF_HOME"

# CUDA preflight, before the ~72GB download: permanent platform mismatches
# (e.g. this image's CUDA newer than the JetPack driver — Tegra has no CUDA
# forward compatibility) crash at CUDA init. Without this check they would
# crash-loop in the serve loop below forever while the app's readiness probe
# (which watches the WebUI port only) keeps reporting healthy. A few retries
# cover slow GPU runtime setup; a persistent failure exits non-zero so a broken
# platform surfaces as a broken app. torch comes with the max-nvidia-full
# image and is only used as the probe here — if a future tag drops it, skip
# rather than fail, since the serve loop still reports the real error.
if python3 -c 'import torch' >/dev/null 2>&1; then
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
    echo "means the JetPack driver is older than the CUDA this image was built" >&2
    echo "against (Tegra has no CUDA forward compatibility) — see 'MAX mode' in" >&2
    echo "the template README for the JetPack requirement." >&2
    exit 1
  fi
else
  echo "torch not present in this image; skipping the CUDA preflight."
fi

# --quantization-encoding is passed explicitly rather than left to
# auto-detection so a mis-set MAX_TARGET_MODEL fails loudly instead of silently
# loading a different precision than the memory budget was sized for. MAX
# serves an OpenAI-compatible API on MAX_SERVE_PORT with no authentication —
# unlike the vLLM mode there is no API-key option, see the README caveat.
SERVE_ARGS=(
  serve
  --model "$MAX_TARGET_MODEL"
  --served-model-name "$SERVED_MODEL_NAME"
  --quantization-encoding "$QUANTIZATION_ENCODING"
  --max-length "$MAX_LENGTH"
)

# Two MAX behaviours need adjusting on Tegra (Jetson). /dev/nvmap exists only
# there, so it distinguishes Jetson from the discrete NVIDIA GPUs this image
# also runs on. Both were verified on a Jetson AGX Thor (JetPack 7.2, MAX
# 26.5 nightly).
if [[ -e /dev/nvmap ]]; then
  # 1. Tegra has no CUDA virtual-memory-management support, so MAX's device
  #    graph capture (which calls vmmCreate) aborts the model worker with
  #    CUDA_ERROR_INVALID_DEVICE right after the model loads. Discrete GPUs
  #    keep capture and the decode speedup that comes with it.
  echo "Tegra detected (/dev/nvmap); disabling MAX device graph capture (Tegra has no CUDA VMM)."
  SERVE_ARGS+=(--no-device-graph-capture)

  # 2. On Tegra, MAX reports GPU free memory as the host's MemFree, which
  #    excludes reclaimable page cache — so its planner sees only what is
  #    unused *right now*, not what the model can actually have. 0.70 of that
  #    understates the budget twice over and rejects large models; 0.95 leaves
  #    the same practical headroom on unified memory. See "MAX mode" in the
  #    template README for the page-cache caveat this implies.
  DEVICE_MEMORY_UTILIZATION="${TEGRA_DEVICE_MEMORY_UTILIZATION:-0.95}"

  mem_free_gib=$(awk '/^MemFree:/ {printf "%.1f", $2/1048576}' /proc/meminfo)
  mem_avail_gib=$(awk '/^MemAvailable:/ {printf "%.1f", $2/1048576}' /proc/meminfo)
  echo "Tegra memory: MemFree ${mem_free_gib} GiB, MemAvailable ${mem_avail_gib} GiB (MAX plans against MemFree)."
  if awk "BEGIN { exit !($mem_free_gib < $mem_avail_gib / 2) }"; then
    echo "WARNING: most of this device's memory is held in reclaimable page cache."
    echo "MAX will refuse to load a model larger than ${mem_free_gib} GiB even though"
    echo "${mem_avail_gib} GiB is actually available. Free it before starting, e.g."
    echo "'sync && echo 3 > /proc/sys/vm/drop_caches' on the device host, or reboot."
  fi
fi

SERVE_ARGS+=(--device-memory-utilization "$DEVICE_MEMORY_UTILIZATION")

echo "First start downloads ~72GB of weights into ${HF_HOME} and compiles the"
echo "model graph into the max-cache volume; the OpenAI API on :${MAX_SERVE_PORT}"
echo "stays down until both finish. Later starts reuse both caches."

# Keep retrying like ../ollama/entrypoint.sh and ../vllm/entrypoint.sh: first
# boot often races network readiness, and a transiently-down network should
# mean visible retry logs rather than a dead container. Repeated quick deaths
# exit non-zero — the compose restart policy then restarts the container with
# backoff, so permanent breakage shows up as a restarting container instead of
# a healthy-looking loop. bash stays PID 1 (no exec) so the loop survives max
# exits; docker stop falls back to the grace-period kill.
FAST_EXIT_SECONDS=90
MAX_CONSECUTIVE_FAST_EXITS=5
fast_exits=0
while true; do
  start=$SECONDS
  if max "${SERVE_ARGS[@]}"; then
    exit 0
  fi
  if (( SECONDS - start < FAST_EXIT_SECONDS )); then
    fast_exits=$(( fast_exits + 1 ))
    if (( fast_exits >= MAX_CONSECUTIVE_FAST_EXITS )); then
      echo "ERROR: max serve died within ${FAST_EXIT_SECONDS}s of starting ${fast_exits} times in a row; treating this as permanent, not transient." >&2
      exit 1
    fi
  else
    fast_exits=0
  fi
  echo "max serve exited; retrying in 15s..."
  sleep 15
done
