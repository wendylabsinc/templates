#!/bin/bash
# record-cameras.sh — record all camera streams to this Mac (the dashboard host),
# time-synced. Each camera's MJPEG stream is pulled via its cloud-tunnel port and
# written to one .mp4. All recorders start together and share one session start
# time, so the files line up on a common timeline. Ctrl-C stops them cleanly.
#
# Add/remove cameras in the CAMERAS list below (name + tunnel stream URL).
# Files land in: ~/camera-dashboard/recordings/<session>/<name>.mp4
set -u

# name  →  MJPEG stream URL (the cloud-tunnel ports the dashboard uses)
CAMERAS=(
  "camera-01 http://localhost:8088/stream"
  "camera-02 http://localhost:8089/stream"
  "camera-05 http://localhost:8092/stream"
  # "camera-03 http://localhost:8090/stream"   # offline – re-enable when up
  # "camera-04 http://localhost:8091/stream"
)

BITRATE="${BITRATE:-4M}"                 # per-camera H.264 bitrate
SESSION="$(date +%Y%m%d-%H%M%S)"
START_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
OUTDIR="$HOME/camera-dashboard/recordings/$SESSION"
mkdir -p "$OUTDIR"
echo "session_start_utc=$START_ISO" > "$OUTDIR/session.txt"

PIDS=()
stop(){ echo; echo "Stopping…"; kill -INT "${PIDS[@]}" 2>/dev/null; wait; echo "Saved → $OUTDIR"; exit 0; }
trap stop INT TERM

echo "Recording ${#CAMERAS[@]} camera(s) → $OUTDIR   (start $START_ISO)"
FPS="${FPS:-30}"
for entry in "${CAMERAS[@]}"; do
  name="${entry%% *}"; url="${entry#* }"
  DUR_OPT=(); [ -n "${DURATION:-}" ] && DUR_OPT=(-t "$DURATION")
  # -reconnect*: survive a brief stream blip without killing the file.
  # -r $FPS before -i: treat the MJPEG stream as constant $FPS → clean CFR
  #   timestamps (no non-monotonic DTS). All recorders start together and use
  #   the same rate, so the files line up on a common timeline.
  ffmpeg -nostdin -loglevel warning -y \
    -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 \
    -f mpjpeg -r "$FPS" -i "$url" \
    -c:v h264_videotoolbox -b:v "$BITRATE" -movflags +faststart \
    -metadata creation_time="$START_ISO" \
    "${DUR_OPT[@]}" \
    "$OUTDIR/$name.mp4" &
  PIDS+=($!)
  echo "  ● $name → $name.mp4  (pid $!)"
done

echo "Press Ctrl-C to stop and finalize all recordings."
wait
