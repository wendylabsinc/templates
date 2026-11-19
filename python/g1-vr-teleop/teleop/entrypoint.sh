#!/bin/sh
# Start the bridge with ONE TLS certificate that survives redeploys.
#
# WebXR needs a secure origin, so the headset must accept this self-signed
# certificate once. Until it has, the page cannot open its websocket or enter
# VR. A new certificate on every deploy would make the headset silently stop
# sending frames after each redeploy, so the certificate lives in the
# /sessions persist volume and is only regenerated in its last 30 days.
set -eu

CERT_DIR="${TELEOP_CERT_DIR:-/sessions/certs}"
if ! mkdir -p "$CERT_DIR" 2>/dev/null; then
  # No persist volume: still start, but the certificate will change on every
  # restart and the headset will ask to accept it again.
  echo "[teleop] cannot write $CERT_DIR; using a temporary certificate"
  CERT_DIR=/tmp/teleop-certs
  mkdir -p "$CERT_DIR"
fi
CERT="$CERT_DIR/cert.pem"
KEY="$CERT_DIR/key.pem"

if [ ! -s "$CERT" ] || [ ! -s "$KEY" ] \
   || ! openssl x509 -checkend 2592000 -noout -in "$CERT" >/dev/null 2>&1; then
  # Name every address the headset might use: each IPv4 address on this host
  # (host networking) plus the hostname and its .local name.
  san="DNS:localhost,DNS:$(hostname),DNS:$(hostname).local"
  for ip in $(ip -o -4 addr show 2>/dev/null | awk '{ split($4, a, "/"); print a[1] }'); do
    san="$san,IP:$ip"
  done
  echo "[teleop] generating a TLS certificate in $CERT_DIR for $san"
  # 397 days: Chromium (and so the Quest browser) rejects any certificate
  # valid for more than 398 days, with no way to proceed.
  openssl req -x509 -newkey rsa:2048 -nodes -days 397 \
    -keyout "$KEY" -out "$CERT" -subj "/CN=wendy-g1-vr-teleop" \
    -addext "subjectAltName=$san" >/dev/null 2>&1
  chmod 600 "$KEY"
else
  echo "[teleop] reusing the TLS certificate in $CERT_DIR"
fi
openssl x509 -noout -fingerprint -sha256 -in "$CERT" | sed 's/^/[teleop] /'

exec python3 bridge/server.py \
  --retarget "$TELEOP_RETARGET" --sink "$TELEOP_SINK" --port "$TELEOP_PORT" \
  --tls-cert "$CERT" --tls-key "$KEY" --tls-port "$TELEOP_TLS_PORT" \
  --control-key "${TELEOP_CONTROL_KEY:-}" \
  --realg1-url "${TELEOP_ONBOARD_URL:-ws://127.0.0.1:8090/ctrl}?key=${TELEOP_CONTROL_KEY:-}"
