"""Synthetic ESP32 CSI sender — emits CSI_DATA UDP datagrams for dev and tests.

Lets you exercise the full app + dashboard with no ESP32 hardware. Real sensors
swap in by pointing them at the same UDP port. The breathing rate is encoded as
a slow sinusoidal modulation of subcarrier amplitude.

Usage:
    python tools/csi_sender.py --host 127.0.0.1 --port 5566 --bpm 15
"""

from __future__ import annotations

import argparse
import math
import random
import socket
import time

DEFAULT_MAC = "aa:bb:cc:dd:ee:01"


def build_csi_line(
    link_id: str, amps_int8: list[int], rssi: int = -50, channel: int = 6, ts: int = 0
) -> str:
    """Build a CSI_DATA CSV line the parser understands.

    ``amps_int8`` is a flat list of interleaved (imag, real) int8 values.
    """
    cols = [
        "CSI_DATA", "0", link_id, str(rssi), "11", "1", "7", "1", "0", "0",
        "0", "0", "0", "1", "-90", "0", str(channel), "1", str(ts), "0",
        str(len(amps_int8)), "0", str(len(amps_int8)),
    ]
    array = "[" + ",".join(str(int(v)) for v in amps_int8) + "]"
    return ",".join(cols) + "," + array


def _clamp8(x: float) -> int:
    return max(-127, min(127, int(round(x))))


def modulated_amps(t: float, bpm: float, n_sub: int, base: float = 50.0,
                   depth: float = 6.0, noise: float = 0.4, rng: random.Random | None = None) -> list[int]:
    """Build an int8 (imag, real) array whose amplitude breathes at ``bpm``."""
    rng = rng or random
    freq = bpm / 60.0
    target = base + depth * math.sin(2 * math.pi * freq * t)
    out: list[int] = []
    for _ in range(n_sub):
        real = _clamp8(target + rng.gauss(0, noise))
        out.extend([0, real])  # imag=0 -> amplitude == |real|
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Synthetic ESP32 CSI UDP sender")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5566)
    ap.add_argument("--bpm", type=float, default=15.0, help="breathing rate to encode")
    ap.add_argument("--rate", type=float, default=20.0, help="frames per second")
    ap.add_argument("--sensors", type=int, default=1)
    ap.add_argument("--subcarriers", type=int, default=32)
    ap.add_argument("--duration", type=float, default=0.0, help="seconds (0 = forever)")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rng = random.Random(0)
    period = 1.0 / args.rate
    start = time.monotonic()
    sent = 0
    print(f"Sending CSI to {args.host}:{args.port} at {args.rate} Hz, breathing {args.bpm} BPM")
    try:
        while True:
            t = time.monotonic() - start
            if args.duration and t >= args.duration:
                break
            for s in range(args.sensors):
                mac = f"aa:bb:cc:dd:ee:{s + 1:02x}"
                line = build_csi_line(mac, modulated_amps(t, args.bpm, args.subcarriers, rng=rng))
                sock.sendto(line.encode(), (args.host, args.port))
                sent += 1
            time.sleep(period)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        print(f"sent {sent} frames")


if __name__ == "__main__":
    main()
