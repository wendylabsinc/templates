"""One headset frame, through the real /teleop websocket, must reach the sink.

Every earlier test of the frame path drove a handler or a helper directly.
None sent a frame through ws_handler's drain loop with the app wired the way
main() wires it -- and that loop wraps the whole per-frame path in one
`except Exception` that prints and moves on. A NameError inside it is
therefore invisible: counters climb, the page says connected, and nothing
reaches the robot. #34 put exactly that in: the row-writing call read `armed`
before the line that assigned it, so every frame raised, and apply() was
never called.

So: build the app as main() does, connect a client, send one frame, and ask
the sink whether it heard. Liveness counters are not the measure; the sink is.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "teleop", "bridge"))

import server  # noqa: E402


class _CountingSink(server.LogSink):
    def __init__(self):
        super().__init__()
        self.applied = 0
        self.onboard_state = "HOLDING"

    async def apply(self, targets):
        self.applied += 1


def _frame():
    return {"hands": [{"hand": "right", "landmarks": [0.0] * 75,
                       "wrist": [0.2, -0.2, -0.05], "quat": [0.0, 0.0, 0.0, 1.0],
                       "fabricated": 0, "source": "hand"}],
            "head": {"pos": [0.0, 1.6, 0.0], "quat": [0.0, 0.0, 0.0, 1.0]}}


async def _send_one_frame(app, sink):
    async with TestClient(TestServer(app)) as client:
        async with client.ws_connect("/teleop") as ws:
            await ws.send_json(_frame())
            for _ in range(50):                 # up to 0.5 s for the drain loop
                await asyncio.sleep(0.01)
                if sink.applied:
                    break
    return sink.applied


def test_a_frame_reaches_the_sink():
    sink = _CountingSink()
    app = server.make_app(server.DebugRetargeter(), sink)
    app["camera"] = None
    assert asyncio.run(_send_one_frame(app, sink)) >= 1, \
        "the frame was received but never applied: the per-frame path raised and was swallowed"


def test_a_frame_reaches_the_sink_with_recording_off():
    sink = _CountingSink()
    app = server.make_app(server.DebugRetargeter(), sink, recording=False)
    app["camera"] = None
    assert app["session"] is None
    assert asyncio.run(_send_one_frame(app, sink)) >= 1
