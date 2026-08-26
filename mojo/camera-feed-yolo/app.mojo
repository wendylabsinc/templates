# Camera streaming + YOLOv8n object detection in Mojo on MAX: wendycam V4L2
# MJPEG capture fanned out over wendynet WebSockets (same client protocol as
# python/camera-feed-yolo), with inference driven through Python interop into
# model.py's hand-built MAX graph (docs/mojo-max-port-findings.md MMF-001/012).
#
# Per frame: MJPEG passes through to clients untouched; at most YOLO_MAX_FPS
# times a second the frame is also TurboJPEG-decoded, letterboxed into a
# Mojo-owned fp32 buffer that numpy wraps as a zero-copy view, run through
# the three MEF-precompiled graphs, and reduced to boxes here (confidence
# filter + NMS + unletterboxing) for the meta JSON that precedes every frame.
from std.ffi import external_call, c_int
from std.os import getenv
from std.pathlib import Path
from std.python import Python, PythonObject
from std.time import perf_counter_ns

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
from wendyvision.jpeg import JpegDecoder
from wendyvision.letterbox import LetterboxMap, letterbox_rgb

comptime DEFAULT_PORT = 9005
comptime FRAME_WIDTH = 1280
comptime FRAME_HEIGHT = 720
comptime LOG_CAP = 200
comptime NC = 80
comptime IOU_THRESHOLD = 0.45
comptime MAX_CANDIDATES = 300
comptime EMPTY_META = (
    '{"detections":0,"inference_ms":0,"classes":{},"boxes":[],"frame_w":0,"frame_h":0}'
)


def fmt_fixed(v: Float64, decimals: Int) raises -> String:
    # Non-negative fixed-point formatting (coords/confidences only).
    var m = 1
    for _ in range(decimals):
        m *= 10
    var scaled = Int(v * Float64(m) + 0.5)
    var whole = scaled // m
    var frac = scaled % m
    var frac_str = String(frac)
    while frac_str.byte_length() < decimals:
        frac_str = "0" + frac_str
    return String(whole) + "." + frac_str


struct Detection(Copyable, Movable):
    var x1: Float64
    var y1: Float64
    var x2: Float64
    var y2: Float64
    var conf: Float64
    var cls: Int

    def __init__(
        out self,
        x1: Float64,
        y1: Float64,
        x2: Float64,
        y2: Float64,
        conf: Float64,
        cls: Int,
    ):
        self.x1 = x1
        self.y1 = y1
        self.x2 = x2
        self.y2 = y2
        self.conf = conf
        self.cls = cls


def iou(a: Detection, b: Detection) -> Float64:
    var x1 = a.x1 if a.x1 > b.x1 else b.x1
    var y1 = a.y1 if a.y1 > b.y1 else b.y1
    var x2 = a.x2 if a.x2 < b.x2 else b.x2
    var y2 = a.y2 if a.y2 < b.y2 else b.y2
    var iw = x2 - x1
    var ih = y2 - y1
    if iw <= 0 or ih <= 0:
        return 0.0
    var inter = iw * ih
    var area_a = (a.x2 - a.x1) * (a.y2 - a.y1)
    var area_b = (b.x2 - b.x1) * (b.y2 - b.y1)
    return inter / (area_a + area_b - inter + 1e-9)


def nms(var cands: List[Detection]) raises -> List[Detection]:
    # Greedy per-class NMS. Candidate counts are small (<= MAX_CANDIDATES),
    # selection sort by confidence keeps this dependency-free.
    var out = List[Detection]()
    var used = List[Bool]()
    for _ in range(len(cands)):
        used.append(False)
    while True:
        var best = -1
        var best_conf = -1.0
        for i in range(len(cands)):
            if not used[i] and cands[i].conf > best_conf:
                best = i
                best_conf = cands[i].conf
        if best < 0:
            break
        used[best] = True
        out.append(cands[best].copy())
        for i in range(len(cands)):
            if not used[i] and cands[i].cls == cands[best].cls:
                if iou(cands[best], cands[i]) > IOU_THRESHOLD:
                    used[i] = True
    return out^


struct Yolo(Movable):
    # in_buf/out_buf are wrapped by numpy as views of Mojo memory: they are
    # allocated once and must NEVER be resized while the session lives.
    var sess: PythonObject
    var in_buf: List[Float32]
    var out_buf: List[Float32]
    var imgsz: Int
    var na: Int
    var names: List[String]
    var decoder: JpegDecoder
    var confidence: Float64
    var min_interval_ns: Int
    var last_infer_ns: Int
    var meta: String
    var infer_count: Int
    var last_infer_ms: Float64

    def __init__(out self) raises:
        # Device + imgsz resolution lives in model.py (it can probe the MAX
        # driver); the buffers here must match the imgsz it picks.
        Python.add_to_path("/app")
        var model_mod = Python.import_module("model")
        var cfg = model_mod.resolve_config()
        var device = cfg[0]
        var imgsz = Int(py=cfg[1])
        if imgsz % 32 != 0 or imgsz < 64:
            raise Error("YOLO_IMGSZ must be a multiple of 32 (>= 64)")
        var na = 0
        for stride in [8, 16, 32]:
            na += (imgsz // stride) * (imgsz // stride)
        self.imgsz = imgsz
        self.na = na

        var max_fps = 3.0
        var fps_env = getenv("YOLO_MAX_FPS")
        if fps_env != "":
            max_fps = Float64(fps_env)
        if max_fps < 0.1:
            max_fps = 0.1
        self.min_interval_ns = Int(1_000_000_000.0 / max_fps)
        self.last_infer_ns = 0
        self.confidence = 0.25
        self.meta = String(EMPTY_META)
        self.infer_count = 0
        self.last_infer_ms = 0.0

        self.names = List[String]()
        var f = open(Path("/app/names.txt"), "r")
        for line in f.read().split("\n"):
            var name = String(line.strip())
            if name != "":
                self.names.append(name)
        f.close()
        if len(self.names) != NC:
            raise Error("names.txt must list exactly 80 classes")

        self.decoder = JpegDecoder()
        self.in_buf = List[Float32](unsafe_uninit_length=imgsz * imgsz * 3)
        self.out_buf = List[Float32](unsafe_uninit_length=84 * na)
        for i in range(len(self.in_buf)):
            self.in_buf[i] = 0
        for i in range(len(self.out_buf)):
            self.out_buf[i] = 0

        print("loading YOLOv8n MAX session (imgsz=" + String(imgsz) + ")...")
        self.sess = model_mod.Session(
            Int(self.in_buf.unsafe_ptr()),
            Int(self.out_buf.unsafe_ptr()),
            imgsz,
            device,
        )
        print("YOLOv8n MAX session ready")

    def due(self, now_ns: Int) -> Bool:
        return now_ns - self.last_infer_ns >= self.min_interval_ns

    def set_confidence(mut self, value: Float64):
        var v = value
        if v < 0.05:
            v = 0.05
        if v > 0.95:
            v = 0.95
        self.confidence = v

    def infer_frame(mut self, jpeg: List[UInt8]) raises:
        var t0 = perf_counter_ns()
        var img = self.decoder.decode_rgb(jpeg)
        var map = letterbox_rgb(
            img.pixels, img.width, img.height, self.imgsz, self.in_buf
        )
        self.sess.infer()
        var infer_ms = Float64(perf_counter_ns() - t0) / 1e6

        # out_buf is (84, na) row-major: rows 0-3 xywh in model px, 4-83
        # class scores. Confidence filter -> unletterbox -> NMS.
        var cands = List[Detection]()
        for a in range(self.na):
            var best_cls = -1
            var best_score = self.confidence
            for c in range(NC):
                var s = Float64(self.out_buf[(4 + c) * self.na + a])
                if s > best_score:
                    best_score = s
                    best_cls = c
            if best_cls < 0:
                continue
            var cx = Float64(self.out_buf[0 * self.na + a])
            var cy = Float64(self.out_buf[1 * self.na + a])
            var w = Float64(self.out_buf[2 * self.na + a])
            var h = Float64(self.out_buf[3 * self.na + a])
            var x1 = map.unmap_x(cx - w / 2)
            var y1 = map.unmap_y(cy - h / 2)
            var x2 = map.unmap_x(cx + w / 2)
            var y2 = map.unmap_y(cy + h / 2)
            if x1 < 0:
                x1 = 0
            if y1 < 0:
                y1 = 0
            if x2 > Float64(img.width):
                x2 = Float64(img.width)
            if y2 > Float64(img.height):
                y2 = Float64(img.height)
            if x2 - x1 < 1 or y2 - y1 < 1:
                continue
            cands.append(Detection(x1, y1, x2, y2, best_score, best_cls))
            if len(cands) >= MAX_CANDIDATES:
                break
        var dets = nms(cands^)

        # Meta JSON matching python/camera-feed-yolo's schema.
        var class_names = List[String]()
        var class_counts = List[Int]()
        var boxes = String("[")
        for i in range(len(dets)):
            var d = dets[i].copy()
            if i > 0:
                boxes += ","
            var name = self.names[d.cls]
            boxes += (
                '{"x1":'
                + fmt_fixed(d.x1, 1)
                + ',"y1":'
                + fmt_fixed(d.y1, 1)
                + ',"x2":'
                + fmt_fixed(d.x2, 1)
                + ',"y2":'
                + fmt_fixed(d.y2, 1)
                + ',"conf":'
                + fmt_fixed(d.conf, 3)
                + ',"cls":'
                + String(d.cls)
                + ',"name":"'
                + json_escape(name)
                + '"}'
            )
            var found = False
            for j in range(len(class_names)):
                if class_names[j] == name:
                    class_counts[j] += 1
                    found = True
            if not found:
                class_names.append(name)
                class_counts.append(1)
        boxes += "]"
        var classes = String("{")
        for j in range(len(class_names)):
            if j > 0:
                classes += ","
            classes += '"' + json_escape(class_names[j]) + '":' + String(class_counts[j])
        classes += "}"
        self.meta = (
            '{"detections":'
            + String(len(dets))
            + ',"inference_ms":'
            + fmt_fixed(infer_ms, 1)
            + ',"classes":'
            + classes
            + ',"boxes":'
            + boxes
            + ',"frame_w":'
            + String(img.width)
            + ',"frame_h":'
            + String(img.height)
            + "}"
        )
        self.last_infer_ns = perf_counter_ns()
        self.last_infer_ms = infer_ms
        self.infer_count += 1


struct AppState(Movable):
    var cameras: List[Camera]  # 0-or-1 slot (poor man's Optional[Camera])
    var preferred_path: String
    var ws_clients: List[c_int]
    var logs: List[String]
    var frames_sent: Int
    var yolo: Yolo

    def __init__(out self, var yolo: Yolo):
        self.cameras = List[Camera]()
        self.preferred_path = String("")
        self.ws_clients = List[c_int]()
        self.logs = List[String]()
        self.frames_sent = 0
        self.yolo = yolo^

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
            '{"mode":"mjpeg-ws-yolo","camera":'
            + cam_desc
            + ',"num_clients":'
            + String(len(state.ws_clients))
            + ',"frames_sent":'
            + String(state.frames_sent)
            + ',"imgsz":'
            + String(state.yolo.imgsz)
            + ',"confidence":'
            + fmt_fixed(state.yolo.confidence, 2)
            + ',"inference_count":'
            + String(state.yolo.infer_count)
            + ',"last_inference_ms":'
            + fmt_fixed(state.yolo.last_infer_ms, 1)
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
            if text.find('"confidence"') >= 0:
                var value = json_find_number(text, "confidence")
                state.yolo.set_confidence(value)
                state.log("confidence threshold set to " + fmt_fixed(state.yolo.confidence, 2))
        return True
    except:
        return False


def pump_frame(mut state: AppState):
    # One camera frame → detect (rate-limited) → meta + frame to every client.
    # Send failure drops that client; capture failure closes the camera
    # (reopened by the retry tick).
    var frame: List[UInt8]
    try:
        frame = state.cameras[0].read_frame()
    except:
        state.log("camera read failed; will reopen")
        state.close_camera()
        return
    if state.yolo.due(perf_counter_ns()):
        try:
            state.yolo.infer_frame(frame)
        except:
            state.log("inference failed on frame (continuing)")
    var meta_bytes = str_bytes(state.yolo.meta)
    var dead = List[c_int]()
    for fd in state.ws_clients:
        try:
            ws_send(fd, WS_TEXT, meta_bytes)
            ws_send(fd, WS_BINARY, frame)
        except:
            dead.append(fd)
    state.frames_sent += 1
    for fd in dead:
        state.drop_client(fd)


def main() raises:
    var port = DEFAULT_PORT
    var port_env = getenv("PORT")
    if port_env != "":
        port = Int(port_env)
    # Model first: the TCP readiness probe should only pass once inference
    # can serve (first boot may compile MEFs — see model.ensure_compiled).
    var state = AppState(Yolo())
    var listener = Listener(port)
    state.log("camera-feed-yolo (mojo) listening on :" + String(port))

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
