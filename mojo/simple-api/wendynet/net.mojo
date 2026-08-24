from std.ffi import external_call, c_int, c_ssize_t


struct Request(Copyable, Movable):
    var method: String
    var path: String
    var head: String
    var body: List[UInt8]

    def __init__(out self, method: String, path: String, head: String, var body: List[UInt8]):
        self.method = method
        self.path = path
        self.head = head
        self.body = body^

    def header(self, name: String) raises -> String:
        # Case-insensitive lookup; values here never contain ':'.
        var target = name.lower() + ":"
        for line in self.head.split("\r\n"):
            var l = String(line)
            if l.lower().startswith(target):
                var parts = l.split(":")
                if len(parts) >= 2:
                    return String(String(parts[1]).strip())
        return String("")


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


def _find_head_end(data: List[UInt8]) -> Int:
    # Byte index just past CRLFCRLF, or -1.
    var i = 0
    while i + 3 < len(data):
        if data[i] == 13 and data[i + 1] == 10 and data[i + 2] == 13 and data[i + 3] == 10:
            return i + 4
        i += 1
    return -1


def read_request(fd: c_int) raises -> Request:
    var data = List[UInt8]()
    var buf = List[UInt8](unsafe_uninit_length=4096)
    var head_end = -1
    while head_end < 0:
        var got = external_call["recv", c_ssize_t](
            fd, buf.unsafe_ptr(), 4096, c_int(0)
        )
        if got <= 0:
            raise Error("peer closed during request head")
        for i in range(Int(got)):
            data.append(buf[i])
        head_end = _find_head_end(data)

    var head_bytes = List[UInt8]()
    for i in range(head_end):
        head_bytes.append(data[i])
    var head = String(from_utf8=head_bytes)

    var request_line = String(head.split("\r\n")[0])
    var parts = request_line.split(" ")
    var method = String(parts[0]) if len(parts) >= 1 else String("GET")
    var path = String(parts[1]) if len(parts) >= 2 else String("/")

    # Body: honor Content-Length (no chunked support).
    var content_length = 0
    var target = String("content-length:")
    for line in head.split("\r\n"):
        var l = String(line)
        if l.lower().startswith(target):
            var vals = l.split(":")
            if len(vals) >= 2:
                content_length = Int(String(String(vals[1]).strip()))
    var body = List[UInt8]()
    for i in range(head_end, len(data)):
        body.append(data[i])
    while len(body) < content_length:
        var got = external_call["recv", c_ssize_t](
            fd, buf.unsafe_ptr(), 4096, c_int(0)
        )
        if got <= 0:
            raise Error("peer closed during body")
        for i in range(Int(got)):
            body.append(buf[i])

    return Request(method, path, head, body^)


def respond(fd: c_int, status: String, content_type: String, body: String) raises:
    var resp: String = (
        "HTTP/1.1 "
        + status
        + "\r\nContent-Type: "
        + content_type
        + "\r\nContent-Length: "
        + String(body.byte_length())
        + "\r\nConnection: close\r\n\r\n"
        + body
    )
    send_all(fd, str_bytes(resp))


def respond_json(fd: c_int, status: String, body: String) raises:
    respond(fd, status, "application/json", body)


struct Listener(Copyable, Movable):
    var fd: c_int

    def __init__(out self, port: Int) raises:
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
        addr[0] = 2  # AF_INET
        addr[2] = UInt8(port >> 8)
        addr[3] = UInt8(port & 0xFF)
        if external_call["bind", c_int](fd, addr.unsafe_ptr(), c_int(16)) != 0:
            raise Error("bind() failed")
        if external_call["listen", c_int](fd, c_int(16)) != 0:
            raise Error("listen() failed")
        self.fd = fd

    def accept(self) raises -> c_int:
        var peer = List[UInt8](unsafe_uninit_length=16)
        var peer_len = List[c_int]()
        peer_len.append(16)
        var conn = external_call["accept", c_int](
            self.fd, peer.unsafe_ptr(), peer_len.unsafe_ptr()
        )
        if conn < 0:
            raise Error("accept() failed")
        return conn

    def close_conn(self, conn: c_int):
        _ = external_call["close", c_int](conn)
