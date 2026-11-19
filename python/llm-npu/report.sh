#!/bin/sh
# What the npu entitlement provided, what this app brought, and whether QNN reaches the
# DSP. serve.py writes this to /report.txt and reads the DSP architecture from it.

echo "=== entitlement-provided (driver-locked only) ==="
echo -n "  transport:   "; ls /opt/wendyos/npu/lib/libcdsprpc.so* 2>/dev/null | tr '\n' ' '; echo
echo -n "  DSP shells:  "; ls /usr/share/qcom/*/*/*/dsp/*/fastrpc_shell* 2>/dev/null | wc -l
echo -n "  conf.d map:  "; ls /usr/share/qcom/conf.d/*.yaml 2>/dev/null | wc -l
echo -n "  fastrpc dev: "; ls /dev/fastrpc-* 2>/dev/null | tr '\n' ' '; echo
echo    "  env:         LD_LIBRARY_PATH=$LD_LIBRARY_PATH FASTRPC_PROCESS_ATTRS=$FASTRPC_PROCESS_ATTRS"
echo
echo "=== app-provided (bundled framework) ==="
echo -n "  genie-t2t-run: "; command -v genie-t2t-run || echo MISSING
echo -n "  QAIRT libs:    "; ls /opt/app/qairt/lib/*.so 2>/dev/null | wc -l
# Named rather than counted: these are the arch-specific halves of the runtime, and a
# missing pair is what "Failed to create device: 14001" actually means.
for arch in V73 V75; do
  echo -n "  $arch stub:      "; ls "/opt/app/qairt/lib/libQnnHtp${arch}Stub.so" 2>/dev/null || echo MISSING
  echo -n "  $arch skel:      "; ls "/app/skel/libQnnHtp${arch}Skel.so" 2>/dev/null || echo MISSING
done
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
qnn-platform-validator --backend dsp --coreVersion --targetPath /tmp/pv 2>&1 \
  | grep -aiE "core version|supported|unit test|failed" | head -4
echo
