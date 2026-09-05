from std.ffi import c_int
from std.os import getenv
from std.pathlib import Path
from wendynet.net import Listener, read_request, respond_json, respond, send_all, str_bytes

def read_file_bytes(path: String) raises -> List[UInt8]:
    var f = open(Path(path), "r")
    var data = f.read_bytes()
    f.close()
    var out = List[UInt8]()
    for b in data:
        out.append(b)
    return out^


def respond_bytes(fd: c_int, content_type: String, var body: List[UInt8]) raises:
    var head: String = (
        "HTTP/1.1 200 OK\r\nContent-Type: "
        + content_type
        + "\r\nContent-Length: "
        + String(len(body))
        + "\r\nConnection: close\r\n\r\n"
    )
    var out = str_bytes(head)
    for b in body:
        out.append(b)
    send_all(fd, out)



def main() raises:
    var port = 3001
    var port_env = getenv("PORT")
    if port_env != "":
        port = Int(port_env)
    var listener = Listener(port)
    print("Listening on port", port)
    while True:
        var conn = listener.accept()
        try:
            var req = read_request(conn)
            if req.method != "GET":
                respond_json(conn, "405 Method Not Allowed", '{"detail":"Use GET"}')
            elif req.path == "/api/hello":
                respond_json(conn, "200 OK", '{"message":"Hello from Wendy!"}')
            elif req.path == "/health":
                respond_json(conn, "200 OK", '{"status":"ok"}')
            else:
                var path = req.path
                if path == "/":
                    path = String("/index.html")
                if path.find("..") >= 0 or not path.startswith("/"):
                    respond_json(conn, "404 Not Found", '{"detail":"Not found"}')
                else:
                    var mime = String("text/html")
                    if path.endswith(".js"):
                        mime = String("application/javascript")
                    elif path.endswith(".css"):
                        mime = String("text/css")
                    respond_bytes(conn, mime, read_file_bytes("/app/static" + path))
        except:
            try:
                respond_json(conn, "404 Not Found", '{"detail":"Not found"}')
            except:
                pass
        listener.close_conn(conn)
