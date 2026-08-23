# wendynet spike: pure-Mojo TCP + HTTP/1.1 + RFC 6455 WebSocket echo server.
# POSIX sockets via libc FFI; SHA-1 hand-rolled (absent from std.hashlib).
from std.base64 import b64encode
from std.ffi import external_call, c_int, c_ssize_t


def rotl(x: UInt32, n: Int) -> UInt32:
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
            w.append(rotl(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1))

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
            var temp = rotl(a, 5) + f + e + k + w[i]
            e = d
            d = c
            c = rotl(b, 30)
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


def str_bytes(s: String) -> List[UInt8]:
    var out = List[UInt8]()
    for b in s.as_bytes():
        out.append(b)
    return out^


def send_all(fd: c_int, data: List[UInt8]) raises:
    var sent = 0
    while sent < len(data):
        var n = external_call["send", c_ssize_t](
            fd, data.unsafe_ptr().unsafe_offset(sent), len(data) - sent, c_int(0)
        )
        if n <= 0:
            raise Error("send() failed")
        sent += Int(n)


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


def read_headers(fd: c_int) raises -> String:
    # Read until CRLFCRLF. Safe for handshakes: the client sends nothing
    # further until it sees our response.
    var data = List[UInt8]()
    var buf = List[UInt8](unsafe_uninit_length=1024)
    while True:
        var got = external_call["recv", c_ssize_t](
            fd, buf.unsafe_ptr(), 1024, c_int(0)
        )
        if got <= 0:
            raise Error("peer closed during headers")
        for i in range(Int(got)):
            data.append(buf[i])
        var s = String(from_utf8=data)
        if s.find("\r\n\r\n") >= 0:
            return s


def header_value(headers: String, name: String) raises -> String:
    # Case-insensitive header lookup (header values here never contain ':').
    var target = name.lower() + ":"
    for line in headers.split("\r\n"):
        var l = String(line)
        if l.lower().startswith(target):
            var parts = l.split(":")
            if len(parts) >= 2:
                return String(String(parts[1]).strip())
    return String("")


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


def ws_session(fd: c_int, headers: String) raises:
    var key = header_value(headers, "Sec-WebSocket-Key")
    var accept_src = str_bytes(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
    var accept = b64encode(sha1(accept_src))
    var resp: String = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        + "Upgrade: websocket\r\n"
        + "Connection: Upgrade\r\n"
        + "Sec-WebSocket-Accept: "
        + accept
        + "\r\n\r\n"
    )
    send_all(fd, str_bytes(resp))

    while True:
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

        if opcode == 0x8:  # close: echo close and end session
            ws_send(fd, 0x8, payload)
            return
        elif opcode == 0x9:  # ping -> pong
            ws_send(fd, 0xA, payload)
        elif opcode == 0x1 or opcode == 0x2:  # echo text/binary
            ws_send(fd, opcode, payload)


def handle(fd: c_int) raises:
    var headers = read_headers(fd)
    if header_value(headers, "Upgrade").lower() == "websocket":
        ws_session(fd, headers)
        return
    var body: String = "wendynet ok"
    var resp: String = (
        "HTTP/1.1 200 OK\r\n"
        + "Content-Type: text/plain\r\n"
        + "Content-Length: "
        + String(body.byte_length())
        + "\r\nConnection: close\r\n\r\n"
        + body
    )
    send_all(fd, str_bytes(resp))


def main() raises:
    # Self-test: RFC 6455 sample key.
    var probe = b64encode(
        sha1(
            str_bytes(
                "dGhlIHNhbXBsZSBub25jZQ==258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            )
        )
    )
    if probe != "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=":
        raise Error("sha1/b64 self-test failed: " + probe)
    print("sha1 self-test OK")

    var port = 9099
    var fd = external_call["socket", c_int](c_int(2), c_int(1), c_int(0))
    if fd < 0:
        raise Error("socket() failed")
    var one = List[c_int]()
    one.append(1)
    _ = external_call["setsockopt", c_int](
        fd, c_int(1), c_int(2), one.unsafe_ptr(), c_int(4)
    )
    var addr = List[UInt8]()
    for _ in range(16):
        addr.append(0)
    addr[0] = 2  # AF_INET (little-endian u16)
    addr[2] = UInt8(port >> 8)  # port, big-endian
    addr[3] = UInt8(port & 0xFF)
    if external_call["bind", c_int](fd, addr.unsafe_ptr(), c_int(16)) != 0:
        raise Error("bind() failed")
    if external_call["listen", c_int](fd, c_int(8)) != 0:
        raise Error("listen() failed")
    print("wendynet echo listening on :", port)

    while True:
        var peer = List[UInt8](unsafe_uninit_length=16)
        var peer_len = List[c_int]()
        peer_len.append(16)
        var conn = external_call["accept", c_int](
            fd, peer.unsafe_ptr(), peer_len.unsafe_ptr()
        )
        if conn < 0:
            continue
        try:
            handle(conn)
        except:
            pass  # connection-level errors must not kill the server
        _ = external_call["close", c_int](conn)
