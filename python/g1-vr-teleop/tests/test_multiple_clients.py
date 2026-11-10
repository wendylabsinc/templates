"""One stale tab must not report the live one as disconnected.

The page reconnects its socket every second while its XR session lives. With
two pages open in the headset's browser -- easy after a few reloads -- one
connecting set `connected` True and the other dropping set it False, twice a
second, while the live page sat there sending nothing. The status read as a
headset flapping on and off, and an evening went into reading that as a
network fault.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))
import server                                                    # noqa: E402

SRC = (ROOT / "teleop" / "bridge" / "server.py").read_text()
BODY = SRC.split("async def ws_handler")[1].split("\nasync def ")[0]


def _connect(stats):
    """The counting the handler does on entry."""
    stats["clients"] = stats.get("clients", 0) + 1
    stats.update(connected=True)


def _disconnect(stats):
    """...and on exit."""
    stats["clients"] = max(0, stats.get("clients", 1) - 1)
    if stats["clients"] == 0:
        stats.update(connected=False)


def test_a_second_client_leaving_does_not_disconnect_the_first():
    """The bug, stated as the property it broke."""
    st = {}
    _connect(st)                      # the live page
    _connect(st)                      # a stale tab
    _disconnect(st)                   # the stale tab drops
    assert st["connected"] is True, "a stale tab reported the live page as gone"
    assert st["clients"] == 1


def test_the_last_one_out_clears_it():
    st = {}
    _connect(st); _connect(st)
    _disconnect(st); _disconnect(st)
    assert st["connected"] is False and st["clients"] == 0


def test_the_count_never_goes_negative():
    """A handler that exits without having incremented -- a refused upgrade,
    say -- must not leave a count that can never reach zero again."""
    st = {}
    _disconnect(st)
    assert st["clients"] == 0


def test_the_handler_counts_rather_than_setting_a_flag():
    assert 'stats["clients"] = stats.get("clients", 0) + 1' in BODY
    assert 'if stats["clients"] == 0:' in BODY, \
        "connected must only be cleared by the LAST client leaving"


def test_the_status_reports_the_count():
    """Two clients is worth seeing: a stale tab that keeps reconnecting is
    indistinguishable from a flapping link unless the number is on the page."""
    handler = SRC.split("async def control_status_handler")[1].split("\nasync def ")[0]
    assert '"xr_clients"' in handler
