#!/bin/sh
set -eu

STATE_DIR="${TS_STATE_DIR:-/var/lib/tailscale}"
AUTHKEY_FILE="${TS_AUTHKEY_FILE:-/data/tailscale-authkey}"
UPSTREAM_PORT="${UPSTREAM_PORT:-3001}"
TS_HOSTNAME="${TS_HOSTNAME:-wendy-bridge}"

mkdir -p "$STATE_DIR" /var/run/tailscale

# Run tailscaled in userspace mode so no NET_ADMIN / TUN device is required.
tailscaled \
    --state="$STATE_DIR/tailscaled.state" \
    --socket=/var/run/tailscale/tailscaled.sock \
    --tun=userspace-networking &
TAILSCALED_PID=$!
trap 'kill "$TAILSCALED_PID" 2>/dev/null || true' INT TERM

# Wait for the auth key to be provided via the shared persist volume.
echo "tailscale-bridge: waiting for auth key at $AUTHKEY_FILE ..."
while [ ! -s "$AUTHKEY_FILE" ]; do
    sleep 5
done

tailscale up \
    --authkey "$(cat "$AUTHKEY_FILE")" \
    --hostname "$TS_HOSTNAME"

# Reverse-proxy the upstream dashboard to the tailnet (HTTPS via tailscale serve).
tailscale serve --bg "http://127.0.0.1:${UPSTREAM_PORT}"

echo "tailscale-bridge: serving 127.0.0.1:${UPSTREAM_PORT} on tailnet as ${TS_HOSTNAME}"
wait "$TAILSCALED_PID"
