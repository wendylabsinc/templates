#!/bin/sh
set -eu

# Pick the device by asking MAX itself — WENDY_HAS_GPU is a build arg and is
# not reliably present as a runtime env var in service containers.
if [ -n "${MAX_DEVICES:-}" ]; then
  DEVICES="${MAX_DEVICES}"
elif [ "$(python3 -c 'from max import driver; print(driver.accelerator_count())' 2>/dev/null || echo 0)" -gt 0 ]; then
  DEVICES=gpu
else
  DEVICES=cpu
  # MMF-016: bf16-safetensors repos cannot currently be served on CPU (bf16 is
  # rejected and float32 hits a backwards-cast bug in MAX 26.5). On CPU-only
  # hosts pick an FP32-native model, e.g. modularai/SmolLM-135M-Instruct-FP32.
  case "${MAX_MODEL}" in
    *FP32*|*fp32*) : ;;
    *)
      echo "WARNING: ${MAX_MODEL} is likely bf16-only and will fail on CPU (MMF-016)." >&2
      echo "         Consider MAX_MODEL=modularai/SmolLM-135M-Instruct-FP32 for CPU hosts." >&2
      ;;
  esac
fi

if [ -n "${HF_TOKEN:-}" ]; then
  export HF_TOKEN
fi

# WendyOS service-group containers have been observed with broken DNS even
# with the network entitlement (single-service apps are fine). Diagnose and
# fall back to public resolvers so the HF download can proceed.
if ! getent hosts huggingface.co >/dev/null 2>&1; then
  echo "DNS broken in container; resolv.conf is:" >&2
  cat /etc/resolv.conf >&2 || true
  ip -brief addr 2>/dev/null | head -5 >&2 || true
  if printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > /etc/resolv.conf 2>/dev/null \
     && getent hosts huggingface.co >/dev/null 2>&1; then
    echo "DNS restored via public resolvers (workaround)" >&2
  else
    # max serve validates the HF repo over the network even when every weight
    # is already in HF_HOME, so an offline device crash-loops on a fully
    # cached model unless HF_HUB_OFFLINE is set.
    MODEL_CACHE="${HF_HOME:-$HOME/.cache/huggingface}/hub/models--$(printf '%s' "${MAX_MODEL}" | sed 's|/|--|g')"
    if [ -d "${MODEL_CACHE}" ]; then
      export HF_HUB_OFFLINE=1
      echo "No egress, but ${MAX_MODEL} is cached; starting with HF_HUB_OFFLINE=1" >&2
    else
      echo "DNS still broken and ${MAX_MODEL} is not cached; model download will fail" >&2
    fi
  fi
fi

echo "max serve: model=${MAX_MODEL} devices=${DEVICES}" \
  "max_length=${MAX_LENGTH} batch=${MAX_BATCH_SIZE}" \
  "device_memory_utilization=${MAX_DEVICE_MEMORY_UTILIZATION}"

# --no-device-graph-capture + VMM=0 (set in the image env): required on Jetson
# iGPUs until upstream #6961 is fixed; harmless elsewhere. First start on a
# fresh volume compiles the model graph (~minutes on Jetson CPUs); later starts
# hit the persisted cache and are fast.
exec max serve \
  --model "${MAX_MODEL}" \
  --devices=${DEVICES} \
  --max-batch-size "${MAX_BATCH_SIZE}" \
  --max-length "${MAX_LENGTH}" \
  --device-memory-utilization "${MAX_DEVICE_MEMORY_UTILIZATION}" \
  --no-device-graph-capture \
  --port "${MAX_PORT:-9011}" \
  ${MAX_SERVE_EXTRA_ARGS:-}
