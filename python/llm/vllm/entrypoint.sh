#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$HF_HOME"

BASE_ARGS=(
  serve "$VLLM_TARGET_MODEL"
  --host 0.0.0.0 --port 8000
  --served-model-name "$SERVED_MODEL_NAME"
  --quantization modelopt
  --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION"
  --max-num-batched-tokens 32768
)

# DFlash needs a vLLM with upstream support (vllm-project/vllm#46853). Probe
# the installed package rather than trusting the container tag, and fall back
# to plain (non-speculative) serving so the app still works either way.
DFLASH_ARGS=()
VLLM_DIR="$(python3 -c 'import vllm, os; print(os.path.dirname(vllm.__file__))')"
if [[ "${DFLASH_DISABLE:-0}" != "1" ]] && grep -rqs "dflash" "$VLLM_DIR"; then
  echo "vLLM build supports DFlash; enabling speculative decoding with ${DFLASH_DRAFT_MODEL}."
  DFLASH_ARGS=(
    --speculative-config "{\"method\": \"dflash\", \"model\": \"${DFLASH_DRAFT_MODEL}\", \"num_speculative_tokens\": ${NUM_SPECULATIVE_TOKENS}}"
    --attention-backend flash_attn
  )
else
  echo "WARNING: this vLLM build has no DFlash support (or DFLASH_DISABLE=1); serving ${VLLM_TARGET_MODEL} without speculative decoding."
fi

echo "First start downloads ~60GB of weights into ${HF_HOME}; the OpenAI API on :8000 stays down until the model finishes loading."

# Keep retrying like ../ollama/entrypoint.sh: first boot often races network
# readiness, and a hard-down network should mean visible retry logs rather
# than a dead container. If an attempt WITH DFlash enabled dies quickly,
# assume the speculative config itself is the problem and demote to plain
# serving. bash stays PID 1 (no exec) so the loop survives vLLM exits;
# docker stop falls back to the grace-period kill.
while true; do
  start=$SECONDS
  if vllm "${BASE_ARGS[@]}" ${DFLASH_ARGS[0]:+"${DFLASH_ARGS[@]}"}; then
    exit 0
  fi
  if (( ${#DFLASH_ARGS[@]} > 0 )) && (( SECONDS - start < 90 )); then
    echo "WARNING: vLLM exited quickly with DFlash enabled; retrying WITHOUT speculative decoding."
    DFLASH_ARGS=()
  fi
  echo "vLLM exited; retrying in 15s..."
  sleep 15
done
