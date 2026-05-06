#!/usr/bin/env bash
# Run an MCP tool against the dog's go2-mcp container.
#
#   ./mcp_call.sh stand                                # no-arg tool
#   ./mcp_call.sh sit
#   ./mcp_call.sh dance
#   ./mcp_call.sh hello
#   ./mcp_call.sh stop
#   ./mcp_call.sh follow_me_on
#   ./mcp_call.sh follow_me_off
#   ./mcp_call.sh get_state                             # returns LowState
#
#   ./mcp_call.sh move '{"vx":0.2,"duration":1.0}'      # tool with args
#   ./mcp_call.sh set_velocity '{"vx":0.2}'
#
# Env overrides:
#   DOG_HOST   default 192.168.0.15      (WiFi); use 192.168.123.18 for wired
#   DOG_USER   default unitree
#
# Exec'd inside the running go2-mcp container so we sidestep macOS's
# Local Network Privacy block. The container has python + mcp installed
# already and reaches its own SSE endpoint on 127.0.0.1:3400.
#
# We pass tool/args via sys.argv (ctr exec doesn't support --env).

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <tool_name> [json_args]" >&2
    echo "examples:" >&2
    echo "  $0 stand" >&2
    echo "  $0 get_state" >&2
    echo "  $0 move '{\"vx\":0.2,\"duration\":1.0}'" >&2
    exit 2
fi

tool="$1"
args_json="${2:-{\}}"
host="${DOG_HOST:-192.168.0.15}"
user="${DOG_USER:-unitree}"

# Unique exec-id per invocation so back-to-back calls don't collide.
exec_id="mcpcall_$$_$(date +%s)"

read -r -d '' PY <<'PY' || true
import asyncio, json, sys
from mcp import ClientSession
from mcp.client.sse import sse_client

TOOL = sys.argv[1]
ARGS = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
URL = "http://127.0.0.1:3400/sse"

async def main():
    async with sse_client(URL) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(TOOL, ARGS)
            for c in res.content:
                txt = getattr(c, "text", None)
                if txt:
                    print(txt)
            if res.isError:
                print("(tool returned isError=True)", file=sys.stderr)
                sys.exit(1)

asyncio.run(main())
PY

ssh -t "${user}@${host}" \
    "sudo ctr -n default t exec --exec-id ${exec_id} go2-mcp \
        python3 -c $(printf '%q' "${PY}") \
                  $(printf '%q' "${tool}") \
                  $(printf '%q' "${args_json}")"
