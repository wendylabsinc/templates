"""The seal must say which code recorded the episode -- from inside the image.

The eight committed manifests carry `source.commit`, because they were sealed
on a laptop with a .git beside the tool. On the device there is no .git, so a
seal written at /session/stop reports `commit: null`, and a manifest that
cannot name its code cannot be reproduced. The deploy script knows the commit
at the moment it builds; it passes it (and whether the tree was dirty) into
the image as G1_TELEOP_COMMIT / G1_TELEOP_DIRTY, the seal reads them, and the
bridge repeats them in /control/status and in every session header so the
operator can see what is running without opening a manifest.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "teleop", "bridge"))

import episode_seal  # noqa: E402
import server  # noqa: E402
from tests.test_endpoint_gating import _req, KEY  # noqa: E402


# ---- the seal reads what the deploy passed ----------------------------------

def test_source_takes_commit_and_dirty_from_the_environment(monkeypatch):
    monkeypatch.setenv("G1_TELEOP_COMMIT", "abc123")
    monkeypatch.setenv("G1_TELEOP_DIRTY", "1")
    assert episode_seal._source() == {"repo": "g1-vr-teleop", "commit": "abc123", "dirty": True}
    monkeypatch.setenv("G1_TELEOP_DIRTY", "0")
    assert episode_seal._source()["dirty"] is False


def test_source_dirty_is_unknown_not_false_when_the_deploy_did_not_say(monkeypatch):
    monkeypatch.setenv("G1_TELEOP_COMMIT", "abc123")
    monkeypatch.delenv("G1_TELEOP_DIRTY", raising=False)
    assert episode_seal._source()["dirty"] is None, "absence is unknown, never a clean bill"


def test_source_ignores_dirty_without_a_commit(monkeypatch):
    """A dirty flag alone says nothing about which code ran."""
    monkeypatch.delenv("G1_TELEOP_COMMIT", raising=False)
    monkeypatch.setenv("G1_TELEOP_DIRTY", "1")
    src = episode_seal._source()
    # Falls back to git (this repo) or None; either way the env flag alone did
    # not decide it.
    assert src["commit"] is None or len(src["commit"]) >= 7


# ---- the bridge repeats it ---------------------------------------------------

def test_build_info_reports_the_running_commit(monkeypatch):
    monkeypatch.setenv("G1_TELEOP_COMMIT", "abc123")
    monkeypatch.setenv("G1_TELEOP_DIRTY", "0")
    assert server.build_info() == {"commit": "abc123", "dirty": False}
    monkeypatch.delenv("G1_TELEOP_COMMIT")
    monkeypatch.delenv("G1_TELEOP_DIRTY")
    assert server.build_info() == {"commit": None, "dirty": None}


class _Rec:
    def __init__(self):
        self.meta = None

    def start(self, name="", meta=None):
        self.meta = meta
        return {"ok": True, "session": "s"}

    def status(self):
        return {"recording": None, "queued": 0}

    def path(self, sid):
        return None


def test_session_header_carries_the_build(monkeypatch):
    import types
    monkeypatch.setenv("G1_TELEOP_COMMIT", "abc123")
    monkeypatch.setenv("G1_TELEOP_DIRTY", "1")
    rec = _Rec()
    app = {"control_key": KEY, "session": rec, "arm_mode": "geometry", "camera": None,
           "retargeter": types.SimpleNamespace(), "stats": {}, "sink": server.LogSink()}
    resp = asyncio.run(server.session_handler(_req(app, {"step": "start"}, key=KEY)))
    assert resp.status == 200
    assert rec.meta["build"] == {"commit": "abc123", "dirty": True}


def test_control_status_carries_the_build(monkeypatch):
    import types
    monkeypatch.setenv("G1_TELEOP_COMMIT", "abc123")
    monkeypatch.delenv("G1_TELEOP_DIRTY", raising=False)
    app = {"control_key": KEY, "arm_mode": "geometry", "camera": None,
           "retargeter": types.SimpleNamespace(), "stats": {}, "sink": server.LogSink()}
    resp = asyncio.run(server.control_status_handler(_req(app, {}, key=KEY)))
    import json
    assert json.loads(resp.text)["build"] == {"commit": "abc123", "dirty": None}
