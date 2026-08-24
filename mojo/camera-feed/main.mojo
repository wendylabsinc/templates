# Webcam streaming server in pure Mojo: wendycam V4L2 MJPEG capture fanned
# out over wendynet WebSockets — same endpoints and client protocol as the
# python/camera-feed (GStreamer) template, no GStreamer required. Cameras
# output MJPEG natively, so frames are passed through with no encode step.
#
# Single-threaded poll(2) loop multiplexing the listener, WebSocket clients,
# and the camera fd. Lazy camera lifecycle: opened when the first client
# connects, closed when the last leaves, reopened with retry if it vanishes
# (USB unplug) while clients remain.
from std.ffi import external_call, c_int
from std.os import getenv
from std.pathlib import Path

from wendynet.jsonmini import json_escape, json_find_string
from wendynet.net import (
    Listener,
    Request,
    read_request,
    respond,
    respond_json,
    send_all,
    str_bytes,
)
from wendynet.poll import POLLERR, POLLHUP, POLLIN, PollSet
from wendynet.ws import (
    WS_BINARY,
    WS_CLOSE,
    WS_PING,
    WS_PONG,
    WS_TEXT,
    ws_handshake,
    ws_recv,
    ws_send,
)
from wendycam.camera import Camera, list_cameras

comptime PORT = {{.PORT}}
comptime FRAME_WIDTH = 1280
comptime FRAME_HEIGHT = 720
comptime LOG_CAP = 200


struct AppState(Movable):
    var cameras: List[Camera]  # 0-or-1 slot (poor man's Optional[Camera])
    var preferred_path: String
    var ws_clients: List[c_int]
    var logs: List[String]
    var frames_sent: Int

    def __init__(out self):
        self.cameras = List[Camera]()
        self.preferred_path = String("")
        self.ws_clients = List[c_int]()
        self.logs = List[String]()
        self.frames_sent = 0

    def log(mut self, msg: String):
        print(msg)
        self.logs.append(msg)
        if len(self.logs) > LOG_CAP:
            var trimmed = List[String]()
            for i in range(len(self.logs) - LOG_CAP, len(self.logs)):
                trimmed.append(self.logs[i])
            self.logs = trimmed^

    def has_camera(self) -> Bool:
        return len(self.cameras) > 0

    def close_camera(mut self):
        if self.has_camera():
            self.cameras[0].close()
            self.cameras = List[Camera]()

    def try_open_camera(mut self) -> Bool:
        # Preferred device first, then anything enumerable.
        if self.has_camera():
            return True
        var candidates = List[String]()
        if self.preferred_path != "":
            candidates.append(self.preferred_path)
        try:
            for info in list_cameras():
                if info.path != self.preferred_path:
                    candidates.append(info.path)
        except:
            pass
        for path in candidates:
            try:
                var cam = Camera(path, FRAME_WIDTH, FRAME_HEIGHT)
                self.log(
                    "camera streaming: "
                    + cam.path
                    + " ("
                    + cam.name
                    + ") "
                    + String(cam.width)
                    + "x"
                    + String(cam.height)
                )
                self.preferred_path = path
                self.cameras.append(cam^)
                return True
            except:
                self.log("camera open failed: " + path)
        return False

    def drop_client(mut self, fd: c_int):
        var kept = List[c_int]()
        for existing in self.ws_clients:
            if existing != fd:
                kept.append(existing)
        self.ws_clients = kept^
        _ = external_call["close", c_int](fd)
        self.log("client removed (total: " + String(len(self.ws_clients)) + ")")
        if len(self.ws_clients) == 0 and self.has_camera():
            self.close_camera()
            self.log("camera stopped (no clients)")


def read_file_bytes(path: String) raises -> List[UInt8]:
    var f = open(Path(path), "r")
    var data = f.read_bytes()
    f.close()
    var out = List[UInt8]()
    for b in data:
        out.append(b)
    return out^


def respond_bytes(
    fd: c_int, content_type: String, var body: List[UInt8]
) raises:
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


def content_type_for(path: String) raises -> String:
    if path.endswith(".svg"):
        return String("image/svg+xml")
    if path.endswith(".png"):
        return String("image/png")
    if path.endswith(".ico"):
        return String("image/x-icon")
    if path.endswith(".html"):
        return String("text/html")
    return String("application/octet-stream")


def cameras_json() raises -> String:
    var out = String("[")
    var first = True
    for info in list_cameras():
        if not first:
            out += ","
        first = False
        out += (
            '{"id":"'
            + json_escape(info.path)
            + '","name":"'
            + json_escape(info.name)
            + '"}'
        )
    return out + "]"


def set_recv_timeout(fd: c_int, seconds: Int):
    # SO_RCVTIMEO so a silent client cannot stall the single-threaded loop.
    var tv = List[UInt8]()
    for _ in range(16):
        tv.append(0)
    tv[0] = UInt8(seconds & 0xFF)
    # SOL_SOCKET=1, SO_RCVTIMEO=20
    _ = external_call["setsockopt", c_int](
        fd, c_int(1), c_int(20), tv.unsafe_ptr(), c_int(16)
    )


def handle_http(mut state: AppState, conn: c_int) raises -> Bool:
    # Returns True when the connection was upgraded to a WebSocket (kept open).
    set_recv_timeout(conn, 2)
    var req = read_request(conn)

    if req.path == "/stream" and req.header("Upgrade").lower() == "websocket":
        ws_handshake(conn, req.header("Sec-WebSocket-Key"))
        state.ws_clients.append(conn)
        state.log("client added (total: " + String(len(state.ws_clients)) + ")")
        return True

    if req.path == "/":
        respond_bytes(conn, "text/html", read_file_bytes("/app/index.html"))
    elif req.path.startswith("/assets/") and req.path.find("..") < 0:
        try:
            respond_bytes(
                conn,
                content_type_for(req.path),
                read_file_bytes("/app" + req.path),
            )
        except:
            respond(conn, "404 Not Found", "text/plain", "not found")
    elif req.path == "/cameras":
        respond_json(conn, "200 OK", cameras_json())
    elif req.path == "/logs":
        var out = String("[")
        for i in range(len(state.logs)):
            if i > 0:
                out += ","
            out += '"' + json_escape(state.logs[i]) + '"'
        respond_json(conn, "200 OK", out + "]")
    elif req.path == "/debug":
        var cam_desc = String("null")
        if state.has_camera():
            cam_desc = (
                '"'
                + json_escape(state.cameras[0].path)
                + " "
                + String(state.cameras[0].width)
                + "x"
                + String(state.cameras[0].height)
                + '"'
            )
        respond_json(
            conn,
            "200 OK",
            '{"mode":"mjpeg-ws","camera":'
            + cam_desc
            + ',"num_clients":'
            + String(len(state.ws_clients))
            + ',"frames_sent":'
            + String(state.frames_sent)
            + "}",
        )
    else:
        respond(conn, "404 Not Found", "text/plain", "not found")
    return False


def handle_ws_message(mut state: AppState, fd: c_int) -> Bool:
    # Returns False when the client must be dropped.
    try:
        var frame = ws_recv(fd)
        if frame.opcode == WS_CLOSE:
            try:
                ws_send(fd, WS_CLOSE, frame.payload)
            except:
                pass
            return False
        if frame.opcode == WS_PING:
            ws_send(fd, WS_PONG, frame.payload)
        elif frame.opcode == WS_TEXT:
            var text = String(from_utf8=frame.payload)
            var target = json_find_string(text, "switch_camera")
            if target != "":
                state.log("switching camera to " + target)
                state.close_camera()
                state.preferred_path = target
        return True
    except:
        return False


def pump_frame(mut state: AppState):
    # One camera frame → every client. Send failure drops that client;
    # capture failure closes the camera (reopened by the retry tick).
    var frame: List[UInt8]
    try:
        frame = state.cameras[0].read_frame()
    except:
        state.log("camera read failed; will reopen")
        state.close_camera()
        return
    var dead = List[c_int]()
    for fd in state.ws_clients:
        try:
            ws_send(fd, WS_BINARY, frame)
        except:
            dead.append(fd)
    state.frames_sent += 1
    for fd in dead:
        state.drop_client(fd)


def main() raises:
    var state = AppState()
    var listener = Listener(PORT)
    state.log("camera-feed (mojo) listening on :" + String(PORT))

    while True:
        var ps = PollSet()
        ps.add(listener.fd, POLLIN)
        for fd in state.ws_clients:
            ps.add(fd, POLLIN)
        var cam_slot = -1
        if state.has_camera() and len(state.ws_clients) > 0:
            ps.add(state.cameras[0].fd, POLLIN)
            cam_slot = ps.size() - 1

        var ready = ps.poll(1000)
        if ready == 0:
            # Maintenance tick: clients waiting but no camera → retry open.
            if len(state.ws_clients) > 0 and not state.has_camera():
                _ = state.try_open_camera()
            continue

        # Snapshot client fds: handlers below mutate state.ws_clients.
        var clients = List[c_int]()
        for fd in state.ws_clients:
            clients.append(fd)

        if (ps.revents(0) & POLLIN) != 0:
            var conn: c_int
            try:
                conn = listener.accept()
            except:
                continue
            var keep_open = False
            try:
                keep_open = handle_http(state, conn)
            except:
                pass
            if not keep_open:
                listener.close_conn(conn)
            elif not state.has_camera():
                _ = state.try_open_camera()

        for i in range(len(clients)):
            var revents = ps.revents(1 + i)
            if (revents & (POLLERR | POLLHUP)) != 0:
                state.drop_client(clients[i])
            elif (revents & POLLIN) != 0:
                if not handle_ws_message(state, clients[i]):
                    state.drop_client(clients[i])

        if cam_slot >= 0 and state.has_camera():
            if (ps.revents(cam_slot) & POLLIN) != 0:
                pump_frame(state)
