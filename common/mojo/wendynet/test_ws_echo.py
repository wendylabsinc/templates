#!/usr/bin/env python3
"""Raw-socket test client for the wendynet WS echo spike (no third-party deps).

Exercises: plain HTTP GET, RFC 6455 handshake (verifies Sec-WebSocket-Accept),
masked text echo, 16-bit extended length echo, ping/pong, clean close.
"""
import base64
import hashlib
import os
import socket
import struct
import sys

HOST = os.environ.get("WS_HOST", "127.0.0.1")
PORT = int(os.environ.get("WS_PORT", "9099"))
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def recv_until(sock, marker):
    data = b""
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            fail(f"connection closed waiting for {marker!r}; got {data!r}")
        data += chunk
    return data


def recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            fail(f"connection closed waiting for {n} bytes")
        data += chunk
    return data


def send_frame(sock, opcode, payload):
    mask = os.urandom(4)
    header = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header += bytes([0x80 | n])
    elif n < 65536:
        header += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        header += bytes([0x80 | 127]) + struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(header + mask + masked)


def recv_frame(sock):
    b0, b1 = recv_exact(sock, 2)
    opcode = b0 & 0x0F
    if b1 & 0x80:
        fail("server frame must not be masked")
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", recv_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", recv_exact(sock, 8))[0]
    return opcode, recv_exact(sock, n)


def test_http():
    with socket.create_connection((HOST, PORT), timeout=10) as s:
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        resp = recv_until(s, b"wendynet ok")
        assert b"200" in resp.split(b"\r\n")[0], resp
    print("PASS: plain HTTP GET /")


def test_ws():
    key = base64.b64encode(os.urandom(16)).decode()
    expect_accept = base64.b64encode(
        hashlib.sha1((key + GUID).encode()).digest()
    ).decode()
    with socket.create_connection((HOST, PORT), timeout=10) as s:
        s.sendall(
            (
                "GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        resp = recv_until(s, b"\r\n\r\n").decode()
        if "101" not in resp.split("\r\n")[0]:
            fail(f"no 101: {resp!r}")
        accept = [
            l.split(":", 1)[1].strip()
            for l in resp.split("\r\n")
            if l.lower().startswith("sec-websocket-accept:")
        ]
        if not accept or accept[0] != expect_accept:
            fail(f"bad accept {accept} != {expect_accept}")
        print("PASS: handshake + Sec-WebSocket-Accept")

        send_frame(s, 0x1, b"hello wendynet")
        op, payload = recv_frame(s)
        if op != 0x1 or payload != b"hello wendynet":
            fail(f"echo mismatch: {op} {payload!r}")
        print("PASS: masked text echo")

        big = bytes(range(256)) * 10  # 2560 bytes -> 16-bit extended length
        send_frame(s, 0x2, big)
        op, payload = recv_frame(s)
        if op != 0x2 or payload != big:
            fail(f"big echo mismatch: {op} len={len(payload)}")
        print("PASS: 2560-byte binary echo (extended length)")

        send_frame(s, 0x9, b"pingdata")
        op, payload = recv_frame(s)
        if op != 0xA or payload != b"pingdata":
            fail(f"pong mismatch: {op} {payload!r}")
        print("PASS: ping -> pong")

        send_frame(s, 0x8, struct.pack(">H", 1000))
        op, _ = recv_frame(s)
        if op != 0x8:
            fail(f"expected close reply, got {op}")
        print("PASS: clean close")


if __name__ == "__main__":
    test_http()
    test_ws()
    print("ALL TESTS PASS")
