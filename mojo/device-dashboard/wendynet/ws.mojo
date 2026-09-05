# RFC 6455 WebSocket server support: handshake (SHA-1 hand-rolled — absent
# from Mojo 1.0 std.hashlib, see MMF-011), frame codec, and per-frame recv.
# Extracted from the ws_echo spike; that spike plus test_ws_echo.py remain the
# conformance suite for this module.
from std.base64 import b64encode
from std.ffi import external_call, c_int, c_ssize_t

from .net import send_all, str_bytes

comptime WS_TEXT: UInt8 = 0x1
comptime WS_BINARY: UInt8 = 0x2
comptime WS_CLOSE: UInt8 = 0x8
comptime WS_PING: UInt8 = 0x9
comptime WS_PONG: UInt8 = 0xA

comptime _WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _rotl(x: UInt32, n: Int) -> UInt32:
    return (x << UInt32(n)) | (x >> UInt32(32 - n))


def sha1(data: List[UInt8]) -> List[UInt8]:
    var h0: UInt32 = 0x67452301
    var h1: UInt32 = 0xEFCDAB89
    var h2: UInt32 = 0x98BADCFE
    var h3: UInt32 = 0x10325476
    var h4: UInt32 = 0xC3D2E1F0

    var msg = List[UInt8]()
    for b in data:
        msg.append(b)
    var bit_len = UInt64(len(data)) * 8
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    for i in range(8):
        msg.append(UInt8((bit_len >> UInt64(56 - 8 * i)) & 0xFF))

    var chunk = 0
    while chunk < len(msg):
        var w = List[UInt32]()
        for i in range(16):
            var j = chunk + 4 * i
            w.append(
                (UInt32(msg[j]) << 24)
                | (UInt32(msg[j + 1]) << 16)
                | (UInt32(msg[j + 2]) << 8)
                | UInt32(msg[j + 3])
            )
        for i in range(16, 80):
            w.append(_rotl(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1))

        var a = h0
        var b = h1
        var c = h2
        var d = h3
        var e = h4
        for i in range(80):
            var f: UInt32
            var k: UInt32
            if i < 20:
                f = (b & c) | ((~b) & d)
                k = 0x5A827999
            elif i < 40:
                f = b ^ c ^ d
                k = 0x6ED9EBA1
            elif i < 60:
                f = (b & c) | (b & d) | (c & d)
                k = 0x8F1BBCDC
            else:
                f = b ^ c ^ d
                k = 0xCA62C1D6
            var temp = _rotl(a, 5) + f + e + k + w[i]
            e = d
            d = c
            c = _rotl(b, 30)
            b = a
            a = temp
        h0 += a
        h1 += b
        h2 += c
        h3 += d
        h4 += e
        chunk += 64

    var out = List[UInt8]()
    for h in [h0, h1, h2, h3, h4]:
        for i in range(4):
            out.append(UInt8((h >> UInt32(24 - 8 * i)) & 0xFF))
    return out^


def recv_exact(fd: c_int, n: Int) raises -> List[UInt8]:
    var out = List[UInt8]()
    var buf = List[UInt8](unsafe_uninit_length=n)
    while len(out) < n:
        var got = external_call["recv", c_ssize_t](
            fd, buf.unsafe_ptr(), n - len(out), c_int(0)
        )
        if got <= 0:
            raise Error("peer closed during recv_exact")
        for i in range(Int(got)):
            out.append(buf[i])
    return out^


def ws_accept_value(key: String) raises -> String:
    # Sec-WebSocket-Accept for a client key (pure; RFC 6455 §4.2.2).
    return b64encode(sha1(str_bytes(key + _WS_GUID)))


def ws_handshake(fd: c_int, key: String) raises:
    var resp: String = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        + "Upgrade: websocket\r\n"
        + "Connection: Upgrade\r\n"
        + "Sec-WebSocket-Accept: "
        + ws_accept_value(key)
        + "\r\n\r\n"
    )
    send_all(fd, str_bytes(resp))


def ws_send(fd: c_int, opcode: UInt8, payload: List[UInt8]) raises:
    var frame = List[UInt8]()
    frame.append(0x80 | opcode)
    var n = len(payload)
    if n < 126:
        frame.append(UInt8(n))
    elif n < 65536:
        frame.append(126)
        frame.append(UInt8((n >> 8) & 0xFF))
        frame.append(UInt8(n & 0xFF))
    else:
        frame.append(127)
        for i in range(8):
            frame.append(UInt8((n >> (56 - 8 * i)) & 0xFF))
    for b in payload:
        frame.append(b)
    send_all(fd, frame)


struct WsFrame(Movable):
    var opcode: UInt8
    var payload: List[UInt8]

    def __init__(out self, opcode: UInt8, var payload: List[UInt8]):
        self.opcode = opcode
        self.payload = payload^


def ws_recv(fd: c_int) raises -> WsFrame:
    # One frame; unmasks client payloads. Blocking — callers multiplex with
    # poll() and only call this when the socket is readable.
    var hdr = recv_exact(fd, 2)
    var opcode = hdr[0] & 0x0F
    var masked = (hdr[1] & 0x80) != 0
    var n = Int(hdr[1] & 0x7F)
    if n == 126:
        var ext = recv_exact(fd, 2)
        n = (Int(ext[0]) << 8) | Int(ext[1])
    elif n == 127:
        var ext = recv_exact(fd, 8)
        n = 0
        for i in range(8):
            n = (n << 8) | Int(ext[i])
    var payload: List[UInt8]
    if masked:
        var mask = recv_exact(fd, 4)
        payload = recv_exact(fd, n)
        for i in range(n):
            payload[i] ^= mask[i % 4]
    else:
        payload = recv_exact(fd, n)
    return WsFrame(opcode, payload^)
