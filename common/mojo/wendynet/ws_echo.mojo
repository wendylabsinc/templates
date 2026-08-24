# wendynet spike, now a thin consumer of the wendynet.ws module: TCP +
# HTTP/1.1 + RFC 6455 WebSocket echo server. test_ws_echo.py is the
# conformance suite for the module's handshake and frame codec.
from std.ffi import external_call, c_int

from wendynet.net import Listener, send_all, str_bytes
from wendynet.ws import (
    WS_CLOSE,
    WS_PING,
    WS_PONG,
    WS_TEXT,
    WS_BINARY,
    ws_accept_value,
    ws_handshake,
    ws_recv,
    ws_send,
)


def read_headers(fd: c_int) raises -> String:
    # Read until CRLFCRLF. Safe for handshakes: the client sends nothing
    # further until it sees our response.
    var data = List[UInt8]()
    var buf = List[UInt8](unsafe_uninit_length=1024)
    while True:
        var got = external_call["recv", Int](fd, buf.unsafe_ptr(), 1024, c_int(0))
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


def ws_session(fd: c_int, headers: String) raises:
    ws_handshake(fd, header_value(headers, "Sec-WebSocket-Key"))
    while True:
        var frame = ws_recv(fd)
        if frame.opcode == WS_CLOSE:  # echo close and end session
            ws_send(fd, WS_CLOSE, frame.payload)
            return
        elif frame.opcode == WS_PING:
            ws_send(fd, WS_PONG, frame.payload)
        elif frame.opcode == WS_TEXT or frame.opcode == WS_BINARY:
            ws_send(fd, frame.opcode, frame.payload)


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
    var probe = ws_accept_value("dGhlIHNhbXBsZSBub25jZQ==")
    if probe != "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=":
        raise Error("sha1/b64 self-test failed: " + probe)
    print("sha1 self-test OK")

    var listener = Listener(9099)
    print("wendynet echo listening on : 9099")
    while True:
        var conn: c_int
        try:
            conn = listener.accept()
        except:
            continue
        try:
            handle(conn)
        except:
            pass  # connection-level errors must not kill the server
        listener.close_conn(conn)
