#!/usr/bin/env bash
# PID 1 for Hermes Agent. It seeds the persisted workspace, configures Wendy MCP
# for Claude Code, supervises rootful BuildKit, and runs the HTTPS console.
set -euo pipefail

mkdir -p /workspace /state/tls /run/buildkit /var/lib/buildkit
mkdir -p /home/hermes

chown -R hermes:hermes /home/hermes /workspace /state

if [ ! -e /workspace/CLAUDE.md ]; then
  cp -a /opt/hermes-workspace/. /workspace/
fi

chown -R hermes:hermes /workspace /state

grant_socket_access() {
  socket_path="${1:-}"
  if [ -n "$socket_path" ] && [ -S "$socket_path" ]; then
    chown hermes:hermes "$socket_path" >/dev/null 2>&1 || true
    chmod ug+rw "$socket_path" >/dev/null 2>&1 || true
  fi
}

hermes-user git config --global --add safe.directory /workspace >/dev/null 2>&1 || true

if [ -n "${WENDY_AGENT_SOCKET:-}" ]; then
  grant_socket_access "$WENDY_AGENT_SOCKET"
  hermes-user wendy mcp setup || echo "warning: 'wendy mcp setup' failed; Claude Code will still have the wendy CLI" >&2
else
  echo "warning: WENDY_AGENT_SOCKET is unset; admin entitlement may be missing" >&2
fi

if [ ! -s "$HTTPS_CERT_PATH" ] || [ ! -s "$HTTPS_KEY_PATH" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$HTTPS_KEY_PATH" \
    -out "$HTTPS_CERT_PATH" \
    -days 825 \
    -subj "/CN=hermes-agent" >/dev/null 2>&1
fi

chown -R hermes:hermes /state

BUILDKITD_PID=""
SERVER_PID=""

start_buildkitd() {
  buildkitd \
    ${BUILDKIT_SNAPSHOTTER:+--oci-worker-snapshotter="$BUILDKIT_SNAPSHOTTER"} \
    >/state/buildkitd.log 2>&1 &
  BUILDKITD_PID=$!
}

start_server() {
  hermes-user node /app/server.mjs &
  SERVER_PID=$!
}

shutdown() {
  kill "$SERVER_PID" "$BUILDKITD_PID" >/dev/null 2>&1 || true
  wait "$SERVER_PID" "$BUILDKITD_PID" >/dev/null 2>&1 || true
  exit 0
}
trap shutdown TERM INT

start_buildkitd

for _ in $(seq 1 20); do
  [ -S /run/buildkit/buildkitd.sock ] && break
  sleep 0.25
done
[ -S /run/buildkit/buildkitd.sock ] || echo "warning: buildkitd socket is not ready yet" >&2
grant_socket_access /run/buildkit/buildkitd.sock

start_server

while true; do
  set +e
  wait -n "$SERVER_PID" "$BUILDKITD_PID"
  status=$?
  set -e

  if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    echo "Hermes web console exited with status $status" >&2
    kill "$BUILDKITD_PID" >/dev/null 2>&1 || true
    wait "$BUILDKITD_PID" >/dev/null 2>&1 || true
    exit "$status"
  fi

  if ! kill -0 "$BUILDKITD_PID" >/dev/null 2>&1; then
    echo "warning: buildkitd exited with status $status; restarting in 1s" >&2
    sleep 1
    start_buildkitd
    for _ in $(seq 1 20); do
      [ -S /run/buildkit/buildkitd.sock ] && break
      sleep 0.25
    done
    grant_socket_access /run/buildkit/buildkitd.sock
  fi
done
