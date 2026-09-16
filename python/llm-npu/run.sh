#!/bin/sh
# Llama 3.2 3B on the Hexagon NPU via Genie. The QAIRT/Genie framework is bundled in this
# image (/opt/app/qairt); the npu entitlement supplies the FastRPC transport at
# /opt/wendyos/npu/lib plus the board's DSP shells and conf.d mapping.

B=/data/genie_bundle
OUT=/app/report.txt
STATUS=/app/status

say() { echo "$1" > "$STATUS"; }

# Serve before doing any work: the first run downloads 2.5GB, and a page that is simply
# unreachable for minutes is indistinguishable from a broken one.
say "starting"
python3 /app/serve.py &

fetch_bundle() {
  HF=https://huggingface.co/Volko76/Llama-3.2-3B-Genie-Compatible-QNN-Binaries/resolve/main/genie_bundle
  mkdir -p "$B"
  for f in llama_v3_2_3b_instruct_part_1_of_3.bin \
           llama_v3_2_3b_instruct_part_2_of_3.bin \
           llama_v3_2_3b_instruct_part_3_of_3.bin \
           tokenizer.json genie_config.json htp_backend_ext_config.json \
           libQnnHtpv73Skel.so libCalculator_skel.so; do
    [ -s "$B/$f" ] && continue
    echo "  fetching $f" >&2
    say "downloading $f"
    # Download to .part and rename only on success: a truncated transfer is non-empty,
    # so writing in place would be kept forever by the check above and never retried.
    if curl -fL --retry 5 --retry-all-errors -s -o "$B/$f.part" "$HF/$f"; then
      mv "$B/$f.part" "$B/$f"
    else
      rm -f "$B/$f.part"
      echo "  FAILED $f" >&2
    fi
  done
  # The published config carries Windows paths; point them at this container.
  sed -i "s|C:\\\\\\\\ai-hub-apps\\\\\\\\tutorials\\\\\\\\llm_on_genie\\\\\\\\genie_bundle\\\\\\\\|$B/|g" "$B/genie_config.json" 2>/dev/null
}

{
  echo "=== entitlement-provided (driver-locked only) ==="
  echo -n "  transport:   "; ls /opt/wendyos/npu/lib/libcdsprpc.so* 2>/dev/null | tr '\n' ' '; echo
  echo -n "  DSP shells:  "; ls /usr/share/qcom/*/*/*/dsp/*/fastrpc_shell* 2>/dev/null | wc -l
  echo -n "  conf.d map:  "; ls /usr/share/qcom/conf.d/*.yaml 2>/dev/null | wc -l
  echo -n "  fastrpc dev: "; ls /dev/fastrpc-* 2>/dev/null | tr '\n' ' '; echo
  echo -n "  env:         "; echo "LD_LIBRARY_PATH=$LD_LIBRARY_PATH FASTRPC_PROCESS_ATTRS=$FASTRPC_PROCESS_ATTRS"
  echo
  echo "=== app-provided (bundled framework) ==="
  echo -n "  genie-t2t-run: "; command -v genie-t2t-run || echo MISSING
  echo -n "  QAIRT libs:    "; ls /opt/app/qairt/lib/*.so 2>/dev/null | wc -l
  # Named rather than counted: these two are the arch-specific halves of the runtime, and
  # their absence is what "Failed to create device: 14001" actually means.
  echo -n "  V75 stub:      "; ls /opt/app/qairt/lib/libQnnHtpV75Stub.so 2>/dev/null || echo MISSING
  echo -n "  V75 skel:      "; ls /app/skel/libQnnHtpV75Skel.so 2>/dev/null || echo MISSING
  echo -n "  our skels are not shadowed: "; ls /usr/share/qcom/*/*/*/dsp/*/libQnnHtpV75Skel.so >/dev/null 2>&1 && echo "board skels visible" || echo "none on board"
  echo
  echo "=== nothing of ours is shadowed ==="
  echo -n "  mounts into /usr/lib or /usr/bin: "; grep -cE " /usr/(lib|bin)" /proc/mounts
  echo -n "  total mounts: "; wc -l < /proc/mounts
  echo
  echo "=== unresolved deps ==="
  for L in /opt/app/qairt/lib/libGenie.so /opt/app/qairt/lib/libQnnHtp.so \
           /opt/app/qairt/bin/genie-t2t-run /opt/wendyos/npu/lib/libcdsprpc.so.1; do
    ldd "$L" 2>/dev/null | grep "not found" | awk -v l="$L" '{printf "  %-42s %s\n", l, $1}'
  done
  echo "  (empty above = all deps satisfied)"
  echo

  echo "=== QNN reaches the DSP (platform validator) ==="
  qnn-platform-validator --backend dsp --coreVersion --targetPath /tmp/pv 2>&1 | grep -aiE "core version|supported|unit test|failed" | head -4
  echo

  echo "=== model bundle ==="
  [ -s "$B/genie_config.json" ] || fetch_bundle
  ls -la "$B" 2>/dev/null | awk 'NR>3 {printf "  %12s  %s\n", $5, $9}' | head -9
  echo

  say "loading the model onto the DSP"
  echo "=== RUNNING LLAMA 3.2 3B ON THE HEXAGON NPU ==="
  if [ -s "$B/genie_config.json" ]; then
    # A single >1GB shared-weights buffer cannot map into this SoC's 32-bit per-session
    # IOVA window, so mmap loading is required here, not an optimisation.
    sed -i 's|"use-mmap": *false|"use-mmap": true|' "$B/genie_config.json" 2>/dev/null

    # The persist volume mounts noexec, so the DSP skel cannot load from there; stage it
    # on the image layer. The board's skels stay visible but ours resolve first.
    SKELDIR=/app/skel
    mkdir -p "$SKELDIR"
    cp -f "$B"/*Skel.so "$B"/*skel.so "$SKELDIR"/ 2>/dev/null

    cd "$B"
    echo "--- prompt: the demo's own prompt.txt ---"
    ADSP_LIBRARY_PATH="$SKELDIR" genie-t2t-run --log verbose \
      -c "$B/genie_config.json" --prompt_file /app/prompt.txt > /app/genie.log 2>&1
    echo "  rc=$?"
    grep -aoE "\[BEGIN\]:.{0,100}|Failed to create device: [0-9]+|Device Creation failure" /app/genie.log | head -3

    echo "--- sustained generation, for throughput ---"
    printf '<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\nYou are a helpful assistant.<|eot_id|><|start_header_id|>user<|end_header_id|>\n\nWrite a paragraph about the ocean.<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n' > /tmp/long.txt
    ADSP_LIBRARY_PATH="$SKELDIR" genie-t2t-run --log verbose \
      -c "$B/genie_config.json" --prompt_file /tmp/long.txt > /app/genie-long.log 2>&1
    echo "  rc=$?"
    grep -aoE "\[BEGIN\]:.{0,160}" /app/genie-long.log | head -1
    grep -aoE "tps-prompt:[0-9.]+ tps-generate:[0-9.]+" /app/genie-long.log | tail -1 | sed 's/^/  /'
  else
    echo "  bundle incomplete"
  fi
} > "$OUT" 2>&1
cat "$OUT"
say ready
wait
