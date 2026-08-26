# Browser-facing control surface for the Unitree Go2 template.
#
# Mojo owns the static UI, MAX accelerator report, and Unitree motion service.
# The camera remains Python because Mojo 1.0 has no WebRTC stack. The browser
# talks to both host-network services through this same-origin proxy.
from std.ffi import external_call, c_int, c_ssize_t
from std.os import getenv
from std.pathlib import Path
from std.sys import has_accelerator

from max.gpu.host import DeviceContext

from wendynet.net import Listener, read_request, respond, respond_json, send_all, str_bytes


comptime DEFAULT_PORT = 3500


def read_file_bytes(path: String) raises -> List[UInt8]:
    var f = open(Path(path), "r")
    var data = f.read_bytes()
    f.close()
    var out = List[UInt8]()
    for byte in data:
        out.append(byte)
    return out^


def respond_bytes(fd: c_int, content_type: String, var body: List[UInt8]) raises:
    var head = (
        "HTTP/1.1 200 OK\r\nContent-Type: "
        + content_type
        + "\r\nContent-Length: "
        + String(len(body))
        + "\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
    )
    var output = str_bytes(head)
    for byte in body:
        output.append(byte)
    send_all(fd, output)


def runtime_report() raises -> String:
    comptime if not has_accelerator():
        return String(
            '{"ok":true,"mojo":"1.0.0","max":"26.5.0",'
            + '"mode":"cpu","accelerator":null}'
        )
    else:
        var context = DeviceContext()
        return String(
            '{"ok":true,"mojo":"1.0.0","max":"26.5.0",'
            + '"mode":"gpu","accelerator":"'
            + context.name()
            + '"}'
        )


def proxy_http(
    browser_fd: c_int,
    upstream_port: Int,
    method: String,
    path: String,
    body: List[UInt8],
) raises:
    # Keep motion commands same-origin without opening the robot API through
    # wildcard CORS. The upstream response is relayed byte-for-byte.
    var upstream_fd = external_call["socket", c_int](c_int(2), c_int(1), c_int(0))
    if upstream_fd < 0:
        raise Error("upstream socket failed")

    var address = List[UInt8]()
    for _ in range(16):
        address.append(0)
    address[0] = 2  # AF_INET
    address[2] = UInt8(upstream_port >> 8)
    address[3] = UInt8(upstream_port & 0xFF)
    address[4] = 127
    address[5] = 0
    address[6] = 0
    address[7] = 1

    if external_call["connect", c_int](
        upstream_fd, address.unsafe_ptr(), c_int(16)
    ) != 0:
        _ = external_call["close", c_int](upstream_fd)
        raise Error("upstream connect failed")

    var request_head = (
        method
        + " "
        + path
        + " HTTP/1.1\r\nHost: 127.0.0.1:"
        + String(upstream_port)
        + "\r\nContent-Type: application/json\r\nContent-Length: "
        + String(len(body))
        + "\r\nConnection: close\r\n\r\n"
    )
    var request_bytes = str_bytes(request_head)
    for byte in body:
        request_bytes.append(byte)
    send_all(upstream_fd, request_bytes)

    var buffer = List[UInt8](unsafe_uninit_length=8192)
    while True:
        var received = external_call["recv", c_ssize_t](
            upstream_fd, buffer.unsafe_ptr(), 8192, c_int(0)
        )
        if received <= 0:
            break
        var chunk = List[UInt8]()
        for index in range(Int(received)):
            chunk.append(buffer[index])
        send_all(browser_fd, chunk)
    _ = external_call["close", c_int](upstream_fd)


def motion_path(path: String) -> String:
    if path == "/api/health":
        return String("/health")
    if path == "/api/state":
        return String("/state")
    if path == "/api/velocity":
        return String("/velocity")
    if path == "/api/move":
        return String("/move")
    if path == "/api/stop":
        return String("/stop")
    if path == "/api/stand":
        return String("/stand")
    if path == "/api/sit":
        return String("/sit")
    if path == "/api/lie":
        return String("/lie")
    if path == "/api/hello":
        return String("/hello")
    if path == "/api/dance":
        return String("/dance")
    return String("")


def main() raises:
    var port = DEFAULT_PORT
    var port_env = getenv("PORT")
    if port_env != "":
        port = Int(port_env)

    var report: String
    try:
        report = runtime_report()
    except error:
        report = (
            '{"ok":false,"mojo":"1.0.0","max":"26.5.0",'
            + '"mode":"unavailable","error":"MAX initialization failed"}'
        )
        print("MAX initialization failed: " + String(error))

    var listener = Listener(port)
    print("go2-rc (Mojo 1.0 + MAX 26.5) listening on :" + String(port))

    while True:
        var connection: c_int
        try:
            connection = listener.accept()
        except:
            continue
        try:
            var request = read_request(connection)
            if request.path == "/":
                respond_bytes(connection, "text/html", read_file_bytes("/app/index.html"))
            elif request.path == "/assets/wendy-logo.svg":
                respond_bytes(
                    connection,
                    "image/svg+xml",
                    read_file_bytes("/app/assets/wendy-logo.svg"),
                )
            elif request.path == "/api/runtime":
                respond_json(connection, "200 OK", report)
            elif request.path == "/api/bark":
                proxy_http(connection, 8000, request.method, "/api/bark", request.body)
            elif motion_path(request.path) != "":
                proxy_http(
                    connection,
                    3201,
                    request.method,
                    motion_path(request.path),
                    request.body,
                )
            elif request.path == "/health":
                respond_json(connection, "200 OK", '{"ok":true}')
            else:
                respond(connection, "404 Not Found", "text/plain", "not found")
        except error:
            print("request failed: " + String(error))
            respond(
                connection,
                "500 Internal Server Error",
                "text/plain",
                "internal server error",
            )
        listener.close_conn(connection)
