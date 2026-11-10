"""Every endpoint that mutates state, costs CPU or serves data takes the key.

/control/*, /wristcal/* and /elbowcal/* have been gated since the key existed.
Three others were not, and any of them can be reached by whatever is on the
office LAN:

  POST /record/start|stop allocates and holds memory; /record/dump serves it
  POST /fingercal/*       rewrites the live finger mapping mid-session
  GET  /session/get       serves recording files off the persist volume

These tests call the handlers directly with a fake request rather than through
an HTTP client: no pytest-aiohttp here, and what is under test is the check at
the top of each handler, not aiohttp's routing.
"""
import asyncio
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))

import server  # noqa: E402

KEY = "s3cret"


class _Retargeter:
    """No cal_start: every handler falls through to its 409."""


class _Recorder:
    def path(self, sid):
        return None

    def status(self):
        return {"recording": False}

    def sessions(self):
        return []


class _Sink:
    def __init__(self, state):
        self._state = state

    def status(self):
        return {"state": self._state}


def _app(sink=None):
    return {"control_key": KEY, "retargeter": _Retargeter(),
            "stats": {}, "session": _Recorder(),
            "sink": sink if sink is not None else server.LogSink()}


def _req(app, path_params, key=None, query=None):
    q = dict(query or {})
    if key is not None:
        q["key"] = key
    return types.SimpleNamespace(app=app, query=q, headers={},
                                 match_info=path_params)


def _call(handler, request):
    return asyncio.run(handler(request))


CASES = [
    ("record/start", server.record_handler, {"step": "start"}),
    ("record/stop", server.record_handler, {"step": "stop"}),
    ("record/dump", server.record_handler, {"step": "dump"}),
    ("fingercal/start", server.fingercal_handler, {"step": "start"}),
    ("fingercal/clear", server.fingercal_handler, {"step": "clear"}),
    ("session/get", server.session_handler, {"step": "get"}),
]


@pytest.mark.parametrize("name,handler,params", CASES, ids=[c[0] for c in CASES])
def test_refused_without_the_key(name, handler, params):
    resp = _call(handler, _req(_app(), params))
    assert resp.status == 403, f"{name} answered {resp.status} to an anonymous caller"


@pytest.mark.parametrize("name,handler,params", CASES, ids=[c[0] for c in CASES])
def test_the_key_check_comes_before_everything_else(name, handler, params):
    """An anonymous caller must not be able to tell a 409 from a 404: the key
    is checked first, so nothing about the bridge's mode leaks."""
    wrong = _call(handler, _req(_app(), params, key="nope"))
    assert wrong.status == 403
    right = _call(handler, _req(_app(), params, key=KEY))
    assert right.status != 403, f"{name} refused the correct key"


@pytest.mark.parametrize("name,handler,params", [
    ("fingercal/dump", server.fingercal_handler, {"step": "dump"}),
    ("session/status", server.session_handler, {"step": "status"}),
    ("session/list", server.session_handler, {"step": "list"}),
], ids=lambda c: c if isinstance(c, str) else "")
def test_read_only_status_stays_open(name, handler, params):
    """The control page polls these without a key; they expose no data an
    operator would mind and change nothing."""
    resp = _call(handler, _req(_app(), params))
    assert resp.status == 200, f"{name} is now locked: {resp.status}"


def test_no_key_configured_means_nothing_is_locked():
    """Unset key must not lock an operator out -- same rule as /control/*."""
    app = _app()
    app["control_key"] = ""
    for name, handler, params in CASES:
        resp = _call(handler, _req(app, params))
        assert resp.status != 403, f"{name} locked with no key configured"
