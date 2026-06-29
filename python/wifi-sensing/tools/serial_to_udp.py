"""Bridge ESP32 ``esp-csi`` serial output to the app's UDP ingest port.

Espressif's esp-csi examples print ``CSI_DATA`` lines over the USB serial port.
This forwards each such line, unchanged, as a UDP datagram to the WendyOS device
— so you can use stock esp-csi firmware with no code changes.

Requires pyserial:  pip install pyserial

Example:
    python tools/serial_to_udp.py --serial /dev/tty.usbmodem1101 --baud 921600 \
        --host 192.168.100.190 --port 5566
"""

from __future__ import annotations

import argparse
import socket

import serial  # pip install pyserial


def main() -> None:
    ap = argparse.ArgumentParser(description="Forward esp-csi serial CSI_DATA lines to UDP")
    ap.add_argument("--serial", required=True, help="serial device, e.g. /dev/tty.usbmodem1101 or COM5")
    ap.add_argument("--baud", type=int, default=921600, help="esp-csi default is 921600")
    ap.add_argument("--host", required=True, help="WendyOS device IP")
    ap.add_argument("--port", type=int, default=5566, help="CSI_UDP_PORT on the device")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ser = serial.Serial(args.serial, args.baud, timeout=1)
    dest = (args.host, args.port)
    print(f"Bridging {args.serial}@{args.baud} -> udp://{args.host}:{args.port}")

    forwarded = 0
    try:
        while True:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.strip()
            if line.startswith(b"CSI_DATA"):
                sock.sendto(line, dest)
                forwarded += 1
                if forwarded % 200 == 0:
                    print(f"forwarded {forwarded} CSI frames")
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
        sock.close()
        print(f"forwarded {forwarded} frames total")


if __name__ == "__main__":
    main()
