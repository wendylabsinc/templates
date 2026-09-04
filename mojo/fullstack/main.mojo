# Fullstack backend in pure Mojo: the python/fullstack FastAPI app rebuilt on
# wendynet HTTP/WebSockets — cars CRUD on SQLite (wendydb), live camera
# (wendycam V4L2 MJPEG) and microphone (wendyaudio ALSA PCM) streams, system
# info via /proc + uname/statvfs FFI, and a GPU route that runs a real Mojo
# kernel (gpudiag) instead of shelling out to nvidia-smi. The React frontend
# is served as static files, byte-identical to the python sibling's build.
#
# Single-threaded poll(2) loop multiplexing the listener, both WebSocket
# client sets, and the camera fd; audio is drained on a 50 ms tick. Devices
# open lazily with the first client and close with the last, reopening with
# retry if they vanish (USB unplug) while clients remain.
from std.ffi import external_call, c_int
from std.os import getenv
from std.pathlib import Path

from carstore import (
    car_create,
    car_delete,
    car_get_json,
    car_update,
    cars_list_json,
)
from gpudiag import gpu_probe_json
from sysinfo import system_json
from wendynet.jsonmini import json_escape, json_find_number, json_find_string
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
from wendyaudio.alsa import AlsaPcm
from wendyaudio.devices import list_capture_devices, list_playback_devices

# PORT comes from the environment (set by the Dockerfile from the scaffold
# variable) so no template token has to live in Mojo source.
comptime DEFAULT_PORT = 9006
comptime FRAME_WIDTH = 1280
comptime FRAME_HEIGHT = 720
comptime SAMPLE_RATE = 16000
comptime CHUNK_FRAMES = 1600  # 100 ms at 16 kHz
comptime PROC_ASOUND = "/proc/asound"
comptime THERMAL_ROOT = "/sys/class/thermal"
comptime STATIC_DIR = "/app/static"
comptime DB_PATH = "/data/cars.db"
comptime LOG_CAP = 200

# handle_http verdicts for the accept path.
comptime CONN_CLOSE = 0
comptime CONN_WS_CAMERA = 1
comptime CONN_WS_AUDIO = 2


struct AppState(Movable):
    var cameras: List[Camera]  # 0-or-1 slot (poor man's Optional[Camera])
    var captures: List[AlsaPcm]  # 0-or-1 slot
    var preferred_cam: String
    var preferred_mic: String
    var cam_clients: List[c_int]
    var audio_clients: List[c_int]
    var logs: List[String]
    var frames_sent: Int
    var chunks_sent: Int
    var cam_retry_ticks: Int
    var mic_retry_ticks: Int
    var gpu_cached: String  # lazy: CUDA context creation is a one-time cost

    def __init__(out self):
        self.cameras = List[Camera]()
        self.captures = List[AlsaPcm]()
        self.preferred_cam = String("")
        self.preferred_mic = String("")
        self.cam_clients = List[c_int]()
        self.audio_clients = List[c_int]()
        self.logs = List[String]()
        self.frames_sent = 0
        self.chunks_sent = 0
        self.cam_retry_ticks = 0
        self.mic_retry_ticks = 0
        self.gpu_cached = String("")

    def log(mut self, msg: String):
        print(msg)
        self.logs.append(msg)
        if len(self.logs) > LOG_CAP:
            var trimmed = List[String]()
            for i in range(len(self.logs) - LOG_CAP, len(self.logs)):
                trimmed.append(self.logs[i])
            self.logs = trimmed^

    # --- camera lifecycle (mojo/camera-feed pattern) ---

    def has_camera(self) -> Bool:
        return len(self.cameras) > 0

    def close_camera(mut self):
        if self.has_camera():
            self.cameras[0].close()
            self.cameras = List[Camera]()

    def try_open_camera(mut self) -> Bool:
        if self.has_camera():
            return True
        var candidates = List[String]()
        if self.preferred_cam != "":
            candidates.append(self.preferred_cam)
        try:
            for info in list_cameras():
                if info.path != self.preferred_cam:
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
                self.preferred_cam = path
                self.cameras.append(cam^)
                return True
            except:
                self.log("camera open failed: " + path)
        return False

    # --- microphone lifecycle (mojo/audio pattern) ---

    def capturing(self) -> Bool:
        return len(self.captures) > 0

    def close_capture(mut self):
        if self.capturing():
            self.captures[0].close()
            self.captures = List[AlsaPcm]()

    def try_open_capture(mut self) -> Bool:
        if self.capturing():
            return True
        var candidates = List[String]()
        if self.preferred_mic != "":
            candidates.append(self.preferred_mic)
        try:
            for m in list_capture_devices(PROC_ASOUND):
                if m.id != self.preferred_mic:
                    candidates.append(m.id)
        except:
            pass
        for dev in candidates:
            try:
                var cap = AlsaPcm.open_capture(dev, SAMPLE_RATE, 1)
                # Preroll check (GStreamer-parity): some devices open cleanly
                # but never produce data (e.g. clockless Jetson APE I2S
                # inputs). Require first samples within ~400 ms or move on.
                var got_data = False
                for _ in range(40):
                    var probe = cap.read_available(CHUNK_FRAMES)
                    if len(probe) > 0:
                        got_data = True
                        break
                    _ = external_call["usleep", c_int](c_int(10000))
                if not got_data:
                    cap.close()
                    self.log("no data from " + dev + "; trying next")
                    continue
                self.log("microphone streaming: " + dev)
                self.preferred_mic = dev
                self.captures.append(cap^)
                return True
            except:
                self.log("microphone open failed: " + dev)
        return False

    # --- client bookkeeping ---

    def drop_cam_client(mut self, fd: c_int):
        var kept = List[c_int]()
        for existing in self.cam_clients:
            if existing != fd:
                kept.append(existing)
        self.cam_clients = kept^
        _ = external_call["close", c_int](fd)
        self.log(
            "camera client removed (total: " + String(len(self.cam_clients)) + ")"
        )
        if len(self.cam_clients) == 0 and self.has_camera():
            self.close_camera()
            self.log("camera stopped (no clients)")

    def drop_audio_client(mut self, fd: c_int):
        var kept = List[c_int]()
        for existing in self.audio_clients:
            if existing != fd:
                kept.append(existing)
        self.audio_clients = kept^
        _ = external_call["close", c_int](fd)
        self.log(
            "audio client removed (total: " + String(len(self.audio_clients)) + ")"
        )
        if len(self.audio_clients) == 0 and self.capturing():
            self.close_capture()
            self.log("audio capture stopped (no clients)")


# ---------------- responses ----------------


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


def respond_no_content(fd: c_int) raises:
    send_all(fd, str_bytes("HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n"))


def respond_not_found(fd: c_int) raises:
    respond_json(fd, "404 Not Found", '{"detail":"Car not found"}')


def content_type_for(path: String) raises -> String:
    if path.endswith(".html"):
        return String("text/html")
    if path.endswith(".js"):
        return String("application/javascript")
    if path.endswith(".css"):
        return String("text/css")
    if path.endswith(".svg"):
        return String("image/svg+xml")
    if path.endswith(".png"):
        return String("image/png")
    if path.endswith(".ico"):
        return String("image/x-icon")
    if path.endswith(".json"):
        return String("application/json")
    if path.endswith(".txt"):
        return String("text/plain")
    if path.endswith(".woff2"):
        return String("font/woff2")
    return String("application/octet-stream")


def serve_spa(conn: c_int, path: String) raises:
    # Static file if it exists, index.html otherwise (client-side routing) —
    # the python sibling's catch-all FileResponse behavior.
    var clean = path
    var q = clean.find("?")
    if q >= 0:
        var trimmed = String(clean[byte=0:q])
        clean = trimmed^
    if clean == "/" or clean == "" or clean.find("..") >= 0:
        clean = String("/index.html")
    try:
        respond_bytes(
            conn, content_type_for(clean), read_file_bytes(STATIC_DIR + clean)
        )
    except:
        respond_bytes(
            conn, "text/html", read_file_bytes(STATIC_DIR + "/index.html")
        )


# ---------------- device listings ----------------


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


def audio_devices_json(capture: Bool) raises -> String:
    var out = String("[")
    var first = True
    var devs = list_capture_devices(PROC_ASOUND) if capture else list_playback_devices(
        PROC_ASOUND
    )
    for d in devs:
        if not first:
            out += ","
        first = False
        out += (
            '{"id":"' + json_escape(d.id) + '","name":"' + json_escape(d.name) + '"}'
        )
    return out + "]"


# ---------------- cars CRUD ----------------


struct CarInput(Movable):
    var make: String
    var model: String
    var color: String
    var year: Int
    var valid: Bool

    def __init__(out self, body: String) raises:
        self.make = json_find_string(body, "make")
        self.model = json_find_string(body, "model")
        self.color = json_find_string(body, "color")
        self.year = Int(json_find_number(body, "year"))
        # pydantic-parity-lite: every field must at least be present.
        self.valid = (
            body.find('"make"') >= 0
            and body.find('"model"') >= 0
            and body.find('"color"') >= 0
            and body.find('"year"') >= 0
        )


def car_id_from(path: String) raises -> Int:
    # /api/cars/<id> -> id; -1 when malformed.
    var raw = String(path[byte=10:])
    try:
        return Int(raw)
    except:
        return -1


def handle_cars(conn: c_int, req: Request) raises:
    var body = String(from_utf8=req.body)
    if req.path == "/api/cars":
        if req.method == "GET":
            respond_json(conn, "200 OK", cars_list_json(DB_PATH))
        elif req.method == "POST":
            var car = CarInput(body)
            if not car.valid:
                respond_json(
                    conn, "422 Unprocessable Entity", '{"detail":"invalid body"}'
                )
                return
            respond_json(
                conn,
                "201 Created",
                car_create(DB_PATH, car.make, car.model, car.color, car.year),
            )
        else:
            respond_json(conn, "405 Method Not Allowed", '{"detail":"method"}')
        return

    var id = car_id_from(req.path)
    if id < 0:
        respond_not_found(conn)
    elif req.method == "GET":
        var row = car_get_json(DB_PATH, id)
        if row == "":
            respond_not_found(conn)
        else:
            respond_json(conn, "200 OK", row)
    elif req.method == "PUT":
        var car = CarInput(body)
        if not car.valid:
            respond_json(
                conn, "422 Unprocessable Entity", '{"detail":"invalid body"}'
            )
            return
        var row = car_update(DB_PATH, id, car.make, car.model, car.color, car.year)
        if row == "":
            respond_not_found(conn)
        else:
            respond_json(conn, "200 OK", row)
    elif req.method == "DELETE":
        if car_delete(DB_PATH, id):
            respond_no_content(conn)
        else:
            respond_not_found(conn)
    else:
        respond_json(conn, "405 Method Not Allowed", '{"detail":"method"}')


# ---------------- http ----------------


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


def handle_http(mut state: AppState, conn: c_int) raises -> Int:
    # CONN_CLOSE, or a CONN_WS_* verdict when upgraded (socket stays open).
    set_recv_timeout(conn, 2)
    var req = read_request(conn)

    if req.header("Upgrade").lower() == "websocket":
        if req.path == "/api/camera/stream":
            ws_handshake(conn, req.header("Sec-WebSocket-Key"))
            state.cam_clients.append(conn)
            state.log(
                "camera client added (total: "
                + String(len(state.cam_clients))
                + ")"
            )
            return CONN_WS_CAMERA
        if req.path == "/api/audio/stream":
            ws_handshake(conn, req.header("Sec-WebSocket-Key"))
            state.audio_clients.append(conn)
            state.log(
                "audio client added (total: "
                + String(len(state.audio_clients))
                + ")"
            )
            return CONN_WS_AUDIO

    if req.path.startswith("/api/cars"):
        handle_cars(conn, req)
    elif req.path == "/api/system":
        respond_json(conn, "200 OK", system_json())
    elif req.path == "/api/gpu":
        if state.gpu_cached == "":
            try:
                state.gpu_cached = gpu_probe_json(THERMAL_ROOT)
            except e:
                state.log("gpu probe failed: " + String(e))
                state.gpu_cached = String('{"available":false}')
            state.log("gpu probe: " + state.gpu_cached)
        respond_json(conn, "200 OK", state.gpu_cached)
    elif req.path == "/api/cameras":
        respond_json(conn, "200 OK", cameras_json())
    elif req.path == "/api/microphones":
        respond_json(conn, "200 OK", audio_devices_json(True))
    elif req.path == "/api/speakers":
        respond_json(conn, "200 OK", audio_devices_json(False))
    elif req.path == "/api/logs":
        var out = String("[")
        for i in range(len(state.logs)):
            if i > 0:
                out += ","
            out += '"' + json_escape(state.logs[i]) + '"'
        respond_json(conn, "200 OK", out + "]")
    elif req.path == "/api/debug":
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
            '{"mode":"fullstack-mojo","camera":'
            + cam_desc
            + ',"capturing":'
            + ("true" if state.capturing() else "false")
            + ',"cam_clients":'
            + String(len(state.cam_clients))
            + ',"audio_clients":'
            + String(len(state.audio_clients))
            + ',"frames_sent":'
            + String(state.frames_sent)
            + ',"chunks_sent":'
            + String(state.chunks_sent)
            + "}",
        )
    elif req.method == "GET":
        serve_spa(conn, req.path)
    else:
        respond(conn, "404 Not Found", "text/plain", "not found")
    return CONN_CLOSE


# ---------------- websocket pumps ----------------


def handle_ws_message(mut state: AppState, fd: c_int) -> Bool:
    # Returns False when the client must be dropped. Switch messages arrive
    # on the matching socket (camera page sends switch_camera, audio page
    # switch_microphone); both are parsed regardless of which list fd is in.
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
            var cam_target = json_find_string(text, "switch_camera")
            if cam_target != "" and cam_target != state.preferred_cam:
                state.log("switching camera to " + cam_target)
                state.close_camera()
                state.preferred_cam = cam_target
                _ = state.try_open_camera()
            var mic_target = json_find_string(text, "switch_microphone")
            if mic_target != "" and mic_target != state.preferred_mic:
                state.log("switching microphone to " + mic_target)
                state.close_capture()
                state.preferred_mic = mic_target
                _ = state.try_open_capture()
        return True
    except:
        return False


def pump_frame(mut state: AppState):
    # One camera frame → every camera client. Send failure drops that client;
    # capture failure closes the camera (reopened by the retry tick).
    var frame: List[UInt8]
    try:
        frame = state.cameras[0].read_frame()
    except:
        state.log("camera read failed; will reopen")
        state.close_camera()
        return
    var dead = List[c_int]()
    for fd in state.cam_clients:
        try:
            ws_send(fd, WS_BINARY, frame)
        except:
            dead.append(fd)
    state.frames_sent += 1
    for fd in dead:
        state.drop_cam_client(fd)


def pump_capture(mut state: AppState):
    if not state.capturing() or len(state.audio_clients) == 0:
        return
    var chunk: List[UInt8]
    try:
        chunk = state.captures[0].read_available(CHUNK_FRAMES)
    except:
        state.log("capture read failed; will reopen")
        state.close_capture()
        return
    if len(chunk) == 0:
        return
    var dead = List[c_int]()
    for fd in state.audio_clients:
        try:
            ws_send(fd, WS_BINARY, chunk)
        except:
            dead.append(fd)
    state.chunks_sent += 1
    for fd in dead:
        state.drop_audio_client(fd)


# ---------------- main loop ----------------


def main() raises:
    var port = DEFAULT_PORT
    var port_env = getenv("PORT")
    if port_env != "":
        port = Int(port_env)

    # /data is the persist-entitlement mount; create it for bare docker runs.
    # mkdirat instead of mkdir: the stdlib may already declare common libc
    # externs, and duplicate declarations fail LLVM lowering (MMF-020).
    var cdata = str_bytes("/data")
    cdata.append(0)
    _ = external_call["mkdirat", c_int](
        c_int(-100), cdata.unsafe_ptr(), c_int(0o755)
    )

    var state = AppState()
    var listener = Listener(port)
    state.log("fullstack (mojo) listening on :" + String(port))

    while True:
        var ps = PollSet()
        ps.add(listener.fd, POLLIN)
        for fd in state.cam_clients:
            ps.add(fd, POLLIN)
        for fd in state.audio_clients:
            ps.add(fd, POLLIN)
        var cam_slot = -1
        if state.has_camera() and len(state.cam_clients) > 0:
            ps.add(state.cameras[0].fd, POLLIN)
            cam_slot = ps.size() - 1

        # Audio is drained on a timer tick rather than fd readiness: 50 ms
        # keeps capture latency under one chunk without ALSA poll-fd
        # plumbing. The camera fd wakes poll() by itself, so an otherwise
        # idle server polls at 1 s.
        var timeout = 50 if state.capturing() else 1000
        var ready = ps.poll(timeout)

        # Maintenance: clients waiting on a device that is not open → retry,
        # throttled to ~1 Hz when the audio tick drives the loop at 50 ms.
        if len(state.audio_clients) > 0 and not state.capturing():
            state.mic_retry_ticks += 1
            if state.mic_retry_ticks >= 10 or timeout == 1000:
                state.mic_retry_ticks = 0
                _ = state.try_open_capture()
        if len(state.cam_clients) > 0 and not state.has_camera():
            state.cam_retry_ticks += 1
            if state.cam_retry_ticks >= 20 or timeout == 1000:
                state.cam_retry_ticks = 0
                _ = state.try_open_camera()

        pump_capture(state)

        if ready == 0:
            continue

        # Snapshot client fds: handlers below mutate the client lists.
        var cams = List[c_int]()
        for fd in state.cam_clients:
            cams.append(fd)
        var auds = List[c_int]()
        for fd in state.audio_clients:
            auds.append(fd)

        if (ps.revents(0) & POLLIN) != 0:
            var conn: c_int
            try:
                conn = listener.accept()
            except:
                continue
            var verdict = CONN_CLOSE
            try:
                verdict = handle_http(state, conn)
            except:
                pass
            if verdict == CONN_CLOSE:
                listener.close_conn(conn)
            elif verdict == CONN_WS_CAMERA and not state.has_camera():
                _ = state.try_open_camera()
            elif verdict == CONN_WS_AUDIO and not state.capturing():
                _ = state.try_open_capture()

        for i in range(len(cams)):
            var revents = ps.revents(1 + i)
            if (revents & (POLLERR | POLLHUP)) != 0:
                state.drop_cam_client(cams[i])
            elif (revents & POLLIN) != 0:
                if not handle_ws_message(state, cams[i]):
                    state.drop_cam_client(cams[i])

        for i in range(len(auds)):
            var revents = ps.revents(1 + len(cams) + i)
            if (revents & (POLLERR | POLLHUP)) != 0:
                state.drop_audio_client(auds[i])
            elif (revents & POLLIN) != 0:
                if not handle_ws_message(state, auds[i]):
                    state.drop_audio_client(auds[i])

        if cam_slot >= 0 and state.has_camera():
            if (ps.revents(cam_slot) & POLLIN) != 0:
                pump_frame(state)
