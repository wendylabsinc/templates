#!/bin/sh
# Launch the dashboard/API and the MCP server together.
#
# The MCP server binds localhost only (MCP_HOST=127.0.0.1); the WendyOS `mcp`
# entitlement makes the agent proxy it over its secure channel, so the MCP port
# is never exposed on the LAN. By default it reads this app's own sensing
# stream; override SENSING_WS to point at another source (e.g. a ruview stream).
set -e

export SENSING_WS="${SENSING_WS:-ws://127.0.0.1:{{.PORT}}/ws/stream}"
export MCP_HOST="${MCP_HOST:-127.0.0.1}"
export MCP_PORT="${MCP_PORT:-8000}"

python /app/mcp/server.py --http &

exec /app/venv/bin/uvicorn app:api --host 0.0.0.0 --port {{.PORT}}
