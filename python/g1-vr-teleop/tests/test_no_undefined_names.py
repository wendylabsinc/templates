"""No undefined name may reach a hot path. This has cost two outages today.

Both were one line, both on the websocket frame loop, both invisible to a
suite of 780 tests because nothing exercises that loop -- it needs aiohttp, a
live websocket and a sink talking to a robot.

  1. `home_on_squeeze(latest, stats, app.get("sink"))` in ws_handler, where
     the local is `request.app`. NameError on the FIRST pose frame: the
     handler died, the headset's socket closed, the page reconnected once a
     second, and hours went into eliminating the certificate, the ports, the
     input filter, stale tabs and the browser -- each by direct test, each
     correct, none of them the answer.

  2. `armed` read in the recorder call and assigned forty lines later.
     UnboundLocalError on every frame WHILE RECORDING, so the socket would
     have died the moment the operator pressed the record button.

A NameError on a frame loop is exactly what a static pass catches for nothing
and an integration test catches expensively, if at all.
"""
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))

pyflakes = pytest.importorskip("pyflakes", reason="pip install pyflakes")

DIRS = ["teleop/bridge", "onboard"]
FILES = sorted(str(p) for d in DIRS for p in (ROOT / d).glob("*.py")
               if not p.name.startswith("_vendor"))


def _report(paths):
    """-> the undefined/unbound lines pyflakes finds, and nothing else.

    Only these two categories: unused imports and shadowed names are style,
    and a suite that fails on style gets switched off.
    """
    out = subprocess.run([sys.executable, "-m", "pyflakes", *paths],
                         capture_output=True, text=True)
    return [l for l in (out.stdout + out.stderr).splitlines()
            if "undefined name" in l or "local variable" in l and "before assignment" in l]


def test_no_undefined_names_anywhere_in_the_source():
    bad = _report(FILES)
    assert not bad, "\n".join(bad)


def test_the_check_catches_both_bugs_it_was_written_for(tmp_path):
    """A check that cannot fail is not a check."""
    p = tmp_path / "x.py"
    p.write_text(
        "async def ws_handler(request):\n"
        "    stats = request.app['stats']\n"
        "    home_on_squeeze(stats, app.get('sink'))\n"
        "    note(contact=(c.state() if armed else None))\n"
        "    armed = str(request.app['sink'].onboard_state)\n")
    bad = "\n".join(_report([str(p)]))
    assert "undefined name 'app'" in bad, bad
    assert "undefined name 'armed'" in bad, bad
