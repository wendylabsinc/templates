#!/usr/bin/env bash
# PID 1 for the Claude Code console. It seeds the persisted workspace, generates
# a self-signed TLS certificate, and runs the HTTPS console as the unprivileged
# `claude` user.
set -euo pipefail

mkdir -p /workspace /state/tls
mkdir -p /home/claude

chown -R claude:claude /home/claude /workspace /state

if [ ! -e /workspace/CLAUDE.md ]; then
  cp -a /opt/claude-workspace/. /workspace/
fi

chown -R claude:claude /workspace /state

claude-user git config --global --add safe.directory /workspace >/dev/null 2>&1 || true

if [ ! -s "$HTTPS_CERT_PATH" ] || [ ! -s "$HTTPS_KEY_PATH" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$HTTPS_KEY_PATH" \
    -out "$HTTPS_CERT_PATH" \
    -days 825 \
    -subj "/CN=claude-console" >/dev/null 2>&1
fi

chown -R claude:claude /state

SERVER_PID=""

shutdown() {
  kill "$SERVER_PID" >/dev/null 2>&1 || true
  wait "$SERVER_PID" >/dev/null 2>&1 || true
  exit 0
}
trap shutdown TERM INT

claude-user node /app/server.mjs &
SERVER_PID=$!

wait "$SERVER_PID"
status=$?
echo "Claude console exited with status $status" >&2
exit "$status"
