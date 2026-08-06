#!/usr/bin/env bash
set -euo pipefail

export HOME=/home/hermes
export USER=hermes
export LOGNAME=hermes

if [ "$(id -u)" = "$(id -u hermes)" ]; then
  exec "$@"
fi

exec runuser -u hermes --preserve-environment -- "$@"
