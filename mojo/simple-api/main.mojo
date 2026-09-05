# simple-api: minimal REST API in pure Mojo on wendynet (vendored from
# common/mojo/wendynet). Mirrors the endpoints of the other language variants:
#   GET /        -> {"message": "hello-world"}
#   GET /health  -> {"status": "ok"}
#   POST /items  -> 201 {"id": 1, "name": ..., "price": ...}
from std.os import getenv

from wendynet import Listener, Request, read_request, respond_json
from wendynet import json_escape, json_find_string, json_find_number


def handle(listener: Listener, conn_fd: Int32, req: Request) raises:
    if req.method == "GET" and req.path == "/":
        print("Received request: GET /")
        respond_json(conn_fd, "200 OK", '{"message": "hello-world"}')
    elif req.method == "GET" and req.path == "/health":
        respond_json(conn_fd, "200 OK", '{"status": "ok"}')
    elif req.method == "POST" and req.path == "/items":
        var body = String(from_utf8=req.body)
        var name = json_find_string(body, "name")
        var price = json_find_number(body, "price")
        print("Received request: POST /items -", name)
        respond_json(
            conn_fd,
            "201 Created",
            '{"id": 1, "name": "'
            + json_escape(name)
            + '", "price": '
            + String(price)
            + "}",
        )
    else:
        respond_json(conn_fd, "404 Not Found", '{"detail": "Not Found"}')


def main() raises:
    var hostname = getenv("WENDY_DEVICE_HOSTNAME")
    if hostname == "":
        hostname = getenv("WENDY_HOSTNAME")
    if hostname == "":
        hostname = "localhost"

    # PORT flows through the Dockerfile's ENV: the wendy CLI's template
    # substitution does not yet cover .mojo files (findings doc, Appendix W).
    var port = 9001
    var port_env = getenv("PORT")
    if port_env != "":
        port = Int(port_env)
    var listener = Listener(port)
    print("Server running on " + hostname + ":" + String(port))

    while True:
        var conn = listener.accept()
        try:
            var req = read_request(conn)
            handle(listener, conn, req)
        except:
            pass
        listener.close_conn(conn)
