import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from .config import BRAIN_BASE_URL, HTTP_TIMEOUT_S, MOTION_BASE_URL

# When running on the dog as its own container, voice-AI (a different
# container) needs to reach us over the network → SSE transport on a TCP
# port. When running locally on the operator's laptop alongside Claude
# Desktop or similar, stdio is the default. Pick via env so the same
# code works both ways.
_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio").lower()
_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
_PORT = int(os.environ.get("MCP_PORT", "3400"))

mcp = FastMCP("go2-mcp", host=_HOST, port=_PORT, streamable_http_path="/")

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)
    return _client


async def _motion_post(path: str, json: dict[str, Any] | None = None) -> str:
    r = await _http().post(f"{MOTION_BASE_URL}{path}", json=json)
    r.raise_for_status()
    return r.text or "ok"


async def _motion_get(path: str) -> str:
    r = await _http().get(f"{MOTION_BASE_URL}{path}")
    r.raise_for_status()
    return r.text


async def _brain_post(path: str) -> str:
    r = await _http().post(f"{BRAIN_BASE_URL}{path}")
    r.raise_for_status()
    return r.text or "ok"


@mcp.tool()
async def stand() -> str:
    """Make the dog stand up from a sitting or lying position."""
    return await _motion_post("/stand")


@mcp.tool()
async def sit() -> str:
    """Make the dog sit."""
    return await _motion_post("/sit")


@mcp.tool()
async def lie() -> str:
    """Make the dog lie down."""
    return await _motion_post("/lie")


@mcp.tool()
async def hello() -> str:
    """Make the dog perform the 'hello' wave gesture."""
    return await _motion_post("/hello")


@mcp.tool()
async def dance() -> str:
    """Make the dog perform the boot dance routine."""
    return await _motion_post("/dance")


@mcp.tool()
async def move(
    vx: float = 0.0,
    vy: float = 0.0,
    vyaw: float = 0.0,
    duration: float = 1.0,
) -> str:
    """
    Walk for a fixed duration. Blocks on the motion service for `duration` seconds.

    vx is forward (m/s), vy is left strafe (m/s), vyaw is yaw rate (rad/s, +left).
    Clamped server-side: |vx|<=0.6, |vy|<=0.4, |vyaw|<=1.0, duration in [0.1, 10].

    Use this for one-shot moves like 'walk forward two seconds'. For continuous
    teleop use set_velocity instead.
    """
    return await _motion_post(
        "/move", {"vx": vx, "vy": vy, "vyaw": vyaw, "duration": duration}
    )


@mcp.tool()
async def set_velocity(vx: float = 0.0, vy: float = 0.0, vyaw: float = 0.0) -> str:
    """
    Fire-and-forget velocity command. Returns immediately.

    The motion service has a 1 s watchdog: if no new velocity arrives within 1 s,
    the dog stops automatically. So either re-issue this every <1 s or treat it
    as a brief twitch. For sustained motion prefer move() with a duration.
    """
    return await _motion_post(
        "/velocity", {"vx": vx, "vy": vy, "vyaw": vyaw}
    )


@mcp.tool()
async def stop() -> str:
    """
    Panic stop. Cancels any in-flight motion AND drops the brain to mock mode
    so a running follow-me loop stops issuing velocity commands.
    """
    motion = await _motion_post("/stop")
    try:
        brain = await _brain_post("/stop")
    except Exception as e:
        return f"motion stopped ({motion}); brain stop failed: {e}"
    return f"motion stopped ({motion}); brain stopped ({brain})"


@mcp.tool()
async def follow_me_on() -> str:
    """
    Start follow-me mode: switch the brain to the unitree controller so it
    actually drives the dog toward the tracked person.

    Requires the brain to be running and connected to a perception source
    (go2-Watchtower on the dog, or go2-sim during dev).
    """
    return await _brain_post("/mode/unitree")


@mcp.tool()
async def follow_me_off() -> str:
    """
    Stop follow-me mode: switch the brain to the mock controller. The brain
    keeps running and logging decisions, but no motion commands reach the dog.
    """
    return await _brain_post("/mode/mock")


@mcp.tool()
async def get_state() -> str:
    """
    Read the latest LowState from the dog: battery SOC, IMU, foot forces,
    joint positions. Useful for 'how much battery left?' style questions.
    """
    return await _motion_get("/state")


def main() -> None:
    # MCP_TRANSPORT=sse  → SSE on $MCP_HOST:$MCP_PORT (containerised deploy)
    # MCP_TRANSPORT=stdio → stdin/stdout (local Claude Desktop, etc.)
    if _TRANSPORT == "sse":
        mcp.run(transport="sse")
    elif _TRANSPORT == "streamable-http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run()  # default stdio


if __name__ == "__main__":
    main()
