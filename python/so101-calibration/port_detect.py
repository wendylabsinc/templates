"""Find the serial port on which the SO-101's Feetech servos answer.

Wendy's `serial` entitlement bind-mounts each tty listed in wendy.json (for
example /dev/ttyACM0) into the container, so the app sees only those devices.
Outside a container, /dev/serial/by-id names are used to label each device.

This module uses only the standard library, so the selection logic can be
tested without pyserial, the Feetech SDK or an arm. The caller supplies
`ping_port(device) -> iterable of servo IDs that answered`; it may raise when
the device cannot be opened.
"""

import glob
import os
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

PATTERNS = ("/dev/serial/by-id/*", "/dev/ttyACM*", "/dev/ttyUSB*")
ENV_NAME = "SO101_CALIB_PORT"

PingPort = Callable[[str], Iterable[int]]


@dataclass(frozen=True)
class Candidate:
    device: str  # node to open, e.g. /dev/ttyACM0
    alias: Optional[str] = None  # /dev/serial/by-id name pointing at it, if any

    def label(self) -> str:
        return f"{self.device} ({self.alias})" if self.alias else self.device


@dataclass(frozen=True)
class ProbeResult:
    candidate: Candidate
    responders: tuple = ()  # servo IDs that answered a ping
    error: Optional[str] = None  # why the device could not be probed


@dataclass(frozen=True)
class Selection:
    device: Optional[str]  # port to open; None when no usable port was found
    message: str  # one line for the web page and the log
    auto: bool  # True when chosen by probing, False when set by ENV_NAME


def configured_port(value: Optional[str]) -> Optional[str]:
    """Return the port set in the environment, or None to auto-detect."""
    if value is None or value.strip().lower() in ("", "auto"):
        return None
    return value.strip()


def _natural_key(path: str) -> list:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", path)]


def candidate_ports(
    patterns: Sequence[str] = PATTERNS,
    glob_fn: Callable[[str], Iterable[str]] = glob.glob,
    realpath_fn: Callable[[str], str] = os.path.realpath,
) -> list:
    """List each distinct serial device once, ordered ttyACM0, ttyACM1, ..., ttyUSB0."""
    aliases: dict = {}
    for pattern in patterns:
        for path in sorted(glob_fn(pattern)):
            device = realpath_fn(path)
            alias = os.path.basename(path) if "/by-id/" in path else None
            if device not in aliases or (alias and aliases[device] is None):
                aliases[device] = alias
    return [Candidate(device, aliases[device]) for device in sorted(aliases, key=_natural_key)]


def probe_ports(candidates: Iterable[Candidate], ping_port: PingPort) -> list:
    results = []
    for candidate in candidates:
        try:
            responders = tuple(ping_port(candidate.device))
        except Exception as exc:  # noqa: BLE001 - any open or I/O failure rules the port out
            results.append(ProbeResult(candidate, (), str(exc) or type(exc).__name__))
            continue
        results.append(ProbeResult(candidate, responders))
    return results


def choose(results: Iterable[ProbeResult]) -> Optional[ProbeResult]:
    """Pick the port where the most servos answered; ties go to the first port."""
    best = None
    for result in results:
        if result.responders and (best is None or len(result.responders) > len(best.responders)):
            best = result
    return best


def _answers(result: ProbeResult, servo_ids: Sequence[int]) -> str:
    total = len(servo_ids)
    count = len(result.responders)
    if count == 0:
        return f"0/{total} servos answered; check that the arm's power supply is on"
    text = f"{count}/{total} servos answered"
    missing = [str(sid) for sid in servo_ids if sid not in result.responders]
    if missing:
        text += f" (no answer from id {', '.join(missing)}; check the servo cables)"
    return text


def select_port(
    configured: Optional[str],
    ping_port: PingPort,
    servo_ids: Sequence[int],
    patterns: Sequence[str] = PATTERNS,
    glob_fn: Callable[[str], Iterable[str]] = glob.glob,
    realpath_fn: Callable[[str], str] = os.path.realpath,
) -> Selection:
    """Use the configured port, or probe every visible serial device for servos."""
    if configured:
        result = probe_ports([Candidate(configured)], ping_port)[0]
        if result.error:
            return Selection(None, f"{configured} (set by {ENV_NAME}) could not be opened: {result.error}", False)
        return Selection(configured, f"{configured} (set by {ENV_NAME}): {_answers(result, servo_ids)}", False)

    results = probe_ports(candidate_ports(patterns, glob_fn, realpath_fn), ping_port)
    best = choose(results)
    if best is not None:
        message = f"auto-detected {best.candidate.label()}: {_answers(best, servo_ids)}"
        others = [r.candidate.device for r in results if r.responders and r is not best]
        if others:
            message += f"; servos also answer on {', '.join(others)} (set {ENV_NAME} to pick that arm)"
        return Selection(best.candidate.device, message, True)

    if not results:
        return Selection(
            None,
            "no serial device visible (looked for " + ", ".join(patterns) + "); "
            "plug in the arm's USB cable and check the serial entitlement in wendy.json",
            True,
        )
    details = "; ".join(
        f"{r.candidate.label()}: {r.error}" if r.error else f"{r.candidate.label()}: no servo answered"
        for r in results
    )
    return Selection(None, f"no Feetech servos answered ({details}); check that the arm's power supply is on", True)
