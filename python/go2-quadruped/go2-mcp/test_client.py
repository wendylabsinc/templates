#!/usr/bin/env python3
"""Quick smoke test for the dog's MCP server.

Connects to the SSE endpoint, lists every tool the server exposes, then
calls `get_state` (read-only — won't move the dog) and prints the
battery / IMU / foot-force snapshot.

Usage:
    pip install mcp                                # one-time
    python3 test_client.py                          # uses default WiFi IP
    MCP_URL=http://192.168.123.18:3400/sse python3 test_client.py   # wired

If you see 11 tools listed and a {"battery_soc": ..., "power_v": ...}
result at the bottom, the whole MCP → motion chain is healthy.
"""

import asyncio
import os
import sys
import traceback

from mcp import ClientSession
from mcp.client.sse import sse_client


URL = os.environ.get("MCP_URL", "http://192.168.0.15:3400/sse")


async def run() -> int:
    print(f"connecting to {URL} ...")
    async with sse_client(URL) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            try:
                info = init.serverInfo
                print(f"connected: server={info.name} v{info.version}")
            except Exception:
                print("connected (server info unavailable)")
            print()

            tools = await session.list_tools()
            print(f"tools ({len(tools.tools)}):")
            for t in tools.tools:
                desc = (t.description or "").strip().split("\n")[0]
                print(f"  {t.name:18s} {desc[:70]}")
            print()

            print("--- get_state (read-only) ---")
            result = await session.call_tool("get_state", {})
            for c in result.content:
                text = getattr(c, "text", None)
                if text:
                    print(text[:600])
            if result.isError:
                print("\n(call returned isError=True — check brain/motion logs)")
                return 1
    return 0


def _flatten(exc):
    """Walk ExceptionGroup / nested __cause__ / __context__ → flat list."""
    seen = []
    stack = [exc]
    while stack:
        e = stack.pop()
        if e is None or e in seen:
            continue
        seen.append(e)
        if isinstance(e, BaseExceptionGroup):
            stack.extend(e.exceptions)
        if e.__cause__ is not None:
            stack.append(e.__cause__)
        if e.__context__ is not None and e.__context__ is not e.__cause__:
            stack.append(e.__context__)
    return seen


async def main() -> int:
    try:
        return await run()
    except BaseException as e:
        print()
        print("ERROR (unwrapped):", file=sys.stderr)
        for exc in _flatten(e):
            print(f"  {type(exc).__name__}: {exc}", file=sys.stderr)
        print("\nFull traceback:", file=sys.stderr)
        traceback.print_exception(type(e), e, e.__traceback__, file=sys.stderr)
        host = URL.split("/")[2].split(":")[0]
        base = URL.rsplit(":", 1)[0]
        print("\nTroubleshooting:", file=sys.stderr)
        print(f"  - Is the dog reachable?   ping {host}", file=sys.stderr)
        print(f"  - Is MCP responding?      curl -i --max-time 3 {URL}",
              file=sys.stderr)
        print(f"  - Is motion alive?        curl -s {base}:3201/health",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
