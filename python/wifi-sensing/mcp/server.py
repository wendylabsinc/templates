"""MCP server exposing live WiFi-sensing data to LLMs.

Lets an LLM (Claude Desktop/Code, etc.) answer questions like "is anyone home?",
"who's moving?", "what's the breathing rate?", "which sensors are online?".

Data source is a sensing WebSocket, configurable via env:
  SENSING_WS   - ws URL of the sensing stream
                 (default ws://localhost:3001/ws/sensing — the wifi-sensing app;
                  point at ruview, e.g. ws://192.168.100.169:3001/ws/sensing)

Run modes:
  python mcp/server.py            # stdio (for local LLM hosts / Claude Desktop)
  python mcp/server.py --http     # streamable-HTTP on :8000/mcp (for remote/in-container)

Requires: mcp, websockets  (see requirements-mcp.txt)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import websockets
from mcp.server.fastmcp import FastMCP

WS_URL = os.environ.get("SENSING_WS", "ws://localhost:3001/ws/sensing")

mcp = FastMCP("wifi-sensing")


async def _latest_frame(timeout: float = 8.0) -> dict:
    """Connect to the sensing WS, return the most recent frame (or {} on failure)."""
    try:
        async with websockets.connect(WS_URL) as ws:
            # take a couple of frames so we report the freshest one
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            try:
                for _ in range(2):
                    frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
            except (asyncio.TimeoutError, Exception):
                pass
            return frame
    except Exception as exc:  # noqa: BLE001 - report cleanly to the LLM
        return {"_error": f"could not reach sensing stream at {WS_URL}: {exc}"}


def _normalize(frame: dict) -> dict:
    """Map either ruview's or the wifi-sensing app's frame into one shape."""
    if "_error" in frame:
        return frame
    # ruview schema
    if "classification" in frame or "estimated_persons" in frame:
        cls = frame.get("classification") or {}
        vs = frame.get("vital_signs") or {}
        nodes = frame.get("nodes") or []
        return {
            "source": "ruview",
            "occupied": bool(cls.get("presence")),
            "motion_level": cls.get("motion_level"),
            "confidence": cls.get("confidence"),
            "person_count": frame.get("estimated_persons"),
            "breathing_bpm": vs.get("breathing_rate_bpm"),
            "heart_bpm": vs.get("heart_rate_bpm"),
            "signal_quality": vs.get("signal_quality"),
            "sensors": [
                {"node_id": n.get("node_id"), "rssi_dbm": n.get("rssi_dbm"),
                 "position": n.get("position")}
                for n in nodes
            ],
        }
    # wifi-sensing app schema
    return {
        "source": "wifi-sensing",
        "occupied": frame.get("occupied"),
        "motion_level": ("moving" if (frame.get("motion") or 0) > 0.5
                         else "still" if frame.get("occupied") else "empty"),
        "motion": frame.get("motion"),
        "breathing_bpm": frame.get("breathing_bpm"),
        "heart_bpm": frame.get("heart_bpm"),
        "sensors": frame.get("sensors") or [],
    }


@mcp.tool()
async def home_status() -> dict:
    """Overall snapshot: is anyone home, how many, motion level, sensors online."""
    f = _normalize(await _latest_frame())
    if "_error" in f:
        return f
    return {
        "anyone_home": f.get("occupied"),
        "person_count": f.get("person_count"),
        "motion_level": f.get("motion_level"),
        "sensors_online": len(f.get("sensors") or []),
        "source": f.get("source"),
    }


@mcp.tool()
async def presence() -> dict:
    """Whether the space is occupied and the current motion level."""
    f = _normalize(await _latest_frame())
    return f if "_error" in f else {
        "occupied": f.get("occupied"),
        "motion_level": f.get("motion_level"),
        "confidence": f.get("confidence"),
    }


@mcp.tool()
async def vitals() -> dict:
    """Estimated breathing and heart rate. Best-effort from CSI; needs a still subject."""
    f = _normalize(await _latest_frame())
    return f if "_error" in f else {
        "breathing_bpm": f.get("breathing_bpm"),
        "heart_bpm": f.get("heart_bpm"),
        "signal_quality": f.get("signal_quality"),
        "note": "CSI-derived estimates; reliable only for a stationary subject.",
    }


@mcp.tool()
async def sensors() -> dict:
    """List the sensor nodes currently streaming, with signal strength."""
    f = _normalize(await _latest_frame())
    return f if "_error" in f else {"count": len(f.get("sensors") or []),
                                     "sensors": f.get("sensors")}


@mcp.tool()
async def raw_frame() -> dict:
    """The latest raw sensing frame (power users / debugging)."""
    return await _latest_frame()


if __name__ == "__main__":
    if "--http" in sys.argv:
        # Bind localhost by default: under the WendyOS `mcp` entitlement the agent
        # dials 127.0.0.1:<port> and proxies the server over its secure channel,
        # so the MCP port is never exposed on the LAN.
        mcp.settings.host = os.environ.get("MCP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.environ.get("MCP_PORT", "8000"))
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
