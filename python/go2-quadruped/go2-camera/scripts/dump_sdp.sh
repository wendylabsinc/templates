#!/usr/bin/env bash
# Dump the running go2-camera WebRTC SDP + transceiver/codec info.
#
# Run from your Mac:
#   ./scripts/dump_sdp.sh
#
# Or pipe through jq if you have it:
#   ./scripts/dump_sdp.sh | jq .
#
# Override the dog hostname/IP via env if needed:
#   DOG=192.168.0.15 ./scripts/dump_sdp.sh
#
# What you're looking for in the output:
#   - "transceivers[*].kind == audio" → the audio path
#   - "direction" / "currentDirection" → "sendrecv" means we're
#     allowed to send AND receive. "recvonly" means our outbound
#     PCM frames are being silently dropped at the SDP layer.
#   - "sender_codecs" → e.g. "audio/opus" / "audio/PCMU" / "audio/PCMA".
#     This is what aiortc encodes our PCM frames into before sending.
#   - "local_sdp" / "remote_sdp" → full SDP for codec/clock-rate
#     verification (look for "a=rtpmap:N opus/48000/2" etc).

set -euo pipefail

DOG="${DOG:-ubuntu.local}"

ssh -t "unitree@${DOG}" \
  'sudo ctr -n default t exec --exec-id sdp1 go2-camera \
       curl -s http://127.0.0.1:8000/api/webrtc_info'
