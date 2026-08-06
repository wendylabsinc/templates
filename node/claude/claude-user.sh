#!/usr/bin/env bash
set -euo pipefail

export HOME=/home/claude
export USER=claude
export LOGNAME=claude

if [ "$(id -u)" = "$(id -u claude)" ]; then
  exec "$@"
fi

exec runuser -u claude --preserve-environment -- "$@"
