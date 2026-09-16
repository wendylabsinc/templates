#!/bin/sh
PORT="${PORT:-{{.PORT}}}"
cd /app

# Pin the loader to the GPU vendor's ICD when there is exactly one real GPU;
# the Mesa package ships ICDs for every vendor and enumerating ones with no
# matching hardware is noise at best.
for icd in /usr/share/vulkan/icd.d/*freedreno*.json /usr/share/vulkan/icd.d/*panfrost*.json; do
  [ -f "$icd" ] && { export VK_ICD_FILENAMES="$icd" VK_DRIVER_FILES="$icd"; break; }
done
echo "[gpu-hello] ICD=${VK_ICD_FILENAMES:-<all>}"

if [ -e /dev/dri/renderD128 ]; then
  echo "[gpu-hello] render node present: $(ls -l /dev/dri/renderD128)"
else
  echo "[gpu-hello] WARNING: /dev/dri/renderD128 missing - is the gpu entitlement declared?"
fi

./gpu-hello > /app/report.json 2>/app/stderr.txt
RC=$?
echo "[gpu-hello] exit=$RC"
cat /app/report.json
[ -s /app/stderr.txt ] && { echo "[gpu-hello] stderr:"; cat /app/stderr.txt; }

exec python3 -m http.server "$PORT"
