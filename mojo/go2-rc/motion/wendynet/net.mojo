from std.ffi import external_call, c_int, c_ssize_t


struct Request(Copyable, Movable):
    var method: String
    var path: String
    var head: String
    var body: List[UInt8]

    def __init__(
        out self,
        method: String,
        path: String,
        head: String,
        var body: List[UInt8],
    ):
        self.method = method
        self.path = path
        self.head = head
        self.body = body^


def str_bytes(s: String) -> List[UInt8]:
    var out = List[UInt8]()
    for byte in s.as_bytes():
        out.append(byte)
    return out^


def send_all(fd: c_int, data: List[UInt8]) raises:
    var sent = 0
    while sent < len(data):
        var count = external_call["send", c_ssize_t](
            fd,
            data.unsafe_ptr().unsafe_offset(sent),
            len(data) - sent,
            c_int(0),
        )
        if count <= 0:
            raise Error("send() failed")
        sent += Int(count)


def _find_head_end(data: List[UInt8]) -> Int:
    var index = 0
    while index + 3 < len(data):
        if (
            data[index] == 13
            and data[index + 1] == 10
            and data[index + 2] == 13
            and data[index + 3] == 10
        ):
            return index + 4
        index += 1
    return -1


def read_request(fd: c_int) raises -> Request:
    var data = List[UInt8]()
    var buffer = List[UInt8](unsafe_uninit_length=4096)
    var head_end = -1
    while head_end < 0:
        var count = external_call["recv", c_ssize_t](
            fd, buffer.unsafe_ptr(), 4096, c_int(0)
        )
        if count <= 0:
            raise Error("peer closed during request head")
        for index in range(Int(count)):
            data.append(buffer[index])
        head_end = _find_head_end(data)

    var head_bytes = List[UInt8]()
    for index in range(head_end):
        head_bytes.append(data[index])
    var head = String(from_utf8=head_bytes)
    var request_line = String(head.split("\r\n")[0])
    var parts = request_line.split(" ")
    var method = String(parts[0]) if len(parts) >= 1 else String("GET")
    var path = String(parts[1]) if len(parts) >= 2 else String("/")

    var content_length = 0
    for line in head.split("\r\n"):
        var value = String(line)
        if value.lower().startswith("content-length:"):
            var fields = value.split(":")
            if len(fields) >= 2:
                content_length = Int(String(String(fields[1]).strip()))
    var body = List[UInt8]()
    for index in range(head_end, len(data)):
        body.append(data[index])
    while len(body) < content_length:
        var count = external_call["recv", c_ssize_t](
            fd, buffer.unsafe_ptr(), 4096, c_int(0)
        )
        if count <= 0:
            raise Error("peer closed during request body")
        for index in range(Int(count)):
            body.append(buffer[index])
    return Request(method, path, head, body^)


def respond(
    fd: c_int, status: String, content_type: String, body: String
) raises:
    var output = (
        "HTTP/1.1 "
        + status
        + "\r\nContent-Type: "
        + content_type
        + "\r\nContent-Length: "
        + String(body.byte_length())
        + "\r\nConnection: close\r\n\r\n"
        + body
    )
    send_all(fd, str_bytes(output))


def respond_json(fd: c_int, status: String, body: String) raises:
    respond(fd, status, "application/json", body)


struct Listener(Copyable, Movable):
    var fd: c_int

    def __init__(out self, port: Int) raises:
        var fd = external_call["socket", c_int](c_int(2), c_int(1), c_int(0))
        if fd < 0:
            raise Error("socket() failed")
        var enabled = List[c_int]()
        enabled.append(1)
        _ = external_call["setsockopt", c_int](
            fd, c_int(1), c_int(2), enabled.unsafe_ptr(), c_int(4)
        )
        var address = List[UInt8]()
        for _ in range(16):
            address.append(0)
        address[0] = 2
        address[2] = UInt8(port >> 8)
        address[3] = UInt8(port & 0xFF)
        if (
            external_call["bind", c_int](fd, address.unsafe_ptr(), c_int(16))
            != 0
        ):
            raise Error("bind() failed")
        if external_call["listen", c_int](fd, c_int(16)) != 0:
            raise Error("listen() failed")
        self.fd = fd

    def accept(self) raises -> c_int:
        var peer = List[UInt8](unsafe_uninit_length=16)
        var peer_length = List[c_int]()
        peer_length.append(16)
        var connection = external_call["accept", c_int](
            self.fd, peer.unsafe_ptr(), peer_length.unsafe_ptr()
        )
        if connection < 0:
            raise Error("accept() failed")
        return connection

    def close_conn(self, connection: c_int):
        _ = external_call["close", c_int](connection)
