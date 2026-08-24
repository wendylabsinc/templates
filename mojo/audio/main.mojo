# Audio streaming server in pure Mojo: wendyaudio ALSA capture (S16LE mono
# 16 kHz) fanned out over wendynet WebSockets, plus .wav playback on a chosen
# speaker — same endpoints and client protocol as the python/audio
# (GStreamer) template, no GStreamer required.
#
# Single-threaded poll(2) loop. ALSA devices are non-blocking: capture is
# drained each tick and broadcast; playback feeds the device as it accepts
# frames. The mic opens on the first client and closes with the last.
from std.ffi import external_call, c_int
from std.os import getenv
from std.pathlib import Path

from wendynet.jsonmini import json_escape, json_find_string
from wendynet.net import (
    Listener,
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
from wendyaudio.alsa import AlsaPcm
from wendyaudio.devices import list_capture_devices, list_playback_devices
from wendyaudio.wav import parse_wav

comptime DEFAULT_PORT = 9004
comptime SAMPLE_RATE = 16000
comptime CHUNK_FRAMES = 1600  # 100 ms at 16 kHz
comptime PROC_ASOUND = "/proc/asound"
comptime ASSETS_DIR = "/app/assets"
comptime LOG_CAP = 200


struct Playback(Movable):
    var pcm: AlsaPcm
    var data: List[UInt8]
    var offset: Int

    def __init__(out self, var pcm: AlsaPcm, var data: List[UInt8], offset: Int):
        self.pcm = pcm^
        self.data = data^
        self.offset = offset


struct AppState(Movable):
    var captures: List[AlsaPcm]  # 0-or-1 slot
    var playbacks: List[Playback]  # 0-or-1 slot
    var preferred_mic: String
    var current_speaker: String
    var ws_clients: List[c_int]
    var logs: List[String]
    var chunks_sent: Int
    var ticks_since_retry: Int

    def __init__(out self):
        self.captures = List[AlsaPcm]()
        self.playbacks = List[Playback]()
        self.preferred_mic = String("")
        self.current_speaker = String("")
        self.ws_clients = List[c_int]()
        self.logs = List[String]()
        self.chunks_sent = 0
        self.ticks_since_retry = 0

    def log(mut self, msg: String):
        print(msg)
        self.logs.append(msg)
        if len(self.logs) > LOG_CAP:
            var trimmed = List[String]()
            for i in range(len(self.logs) - LOG_CAP, len(self.logs)):
                trimmed.append(self.logs[i])
            self.logs = trimmed^

    def capturing(self) -> Bool:
        return len(self.captures) > 0

    def playing(self) -> Bool:
        return len(self.playbacks) > 0

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
                self.log("microphone streaming: " + dev)
                self.preferred_mic = dev
                self.captures.append(cap^)
                return True
            except:
                self.log("microphone open failed: " + dev)
        return False

    def drop_client(mut self, fd: c_int):
        var kept = List[c_int]()
        for existing in self.ws_clients:
            if existing != fd:
                kept.append(existing)
        self.ws_clients = kept^
        _ = external_call["close", c_int](fd)
        self.log("client removed (total: " + String(len(self.ws_clients)) + ")")
        if len(self.ws_clients) == 0 and self.capturing():
            self.close_capture()
            self.log("audio capture stopped (no clients)")


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


def content_type_for(path: String) raises -> String:
    if path.endswith(".svg"):
        return String("image/svg+xml")
    if path.endswith(".wav"):
        return String("audio/wav")
    if path.endswith(".png"):
        return String("image/png")
    if path.endswith(".html"):
        return String("text/html")
    return String("application/octet-stream")


def devices_json(capture: Bool) raises -> String:
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


def title_case(stem: String) raises -> String:
    # ASCII-only: template sound files are named like "car-horn.wav".
    var out = List[UInt8]()
    var new_word = True
    for b in stem.as_bytes():
        var c = Int(b)
        if c == 45 or c == 95:  # '-' or '_'
            out.append(32)
            new_word = True
        elif new_word and c >= 97 and c <= 122:
            out.append(UInt8(c - 32))
            new_word = False
        else:
            out.append(b)
            new_word = False
    return String(from_utf8=out)


def sounds_json() raises -> String:
    var out = String("[")
    var first = True
    var names = List[String]()
    try:
        for entry in Path(ASSETS_DIR).listdir():
            var n = String(entry)
            if n.endswith(".wav"):
                names.append(n)
    except:
        pass
    # Path.listdir order is not specified; sort for a stable UI.
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if names[j] < names[i]:
                var tmp = names[i]
                names[i] = names[j]
                names[j] = tmp
    for n in names:
        if not first:
            out += ","
        first = False
        var stem = String(n[byte = 0 : n.byte_length() - 4])
        out += (
            '{"name":"'
            + json_escape(title_case(stem))
            + '","file":"'
            + json_escape(n)
            + '"}'
        )
    return out + "]"


def start_playback(mut state: AppState, filename: String) raises:
    var wav_bytes = read_file_bytes(ASSETS_DIR + "/" + filename)
    var info = parse_wav(wav_bytes)
    var device = state.current_speaker
    if device == "":
        var speakers = list_playback_devices(PROC_ASOUND)
        if len(speakers) == 0:
            raise Error("no playback devices")
        device = speakers[0].id
    # Replace any in-flight playback.
    if state.playing():
        state.playbacks[0].pcm.close()
        state.playbacks = List[Playback]()
    var pcm = AlsaPcm.open_playback(device, info.rate, info.channels)
    var pcm_data = List[UInt8]()
    for i in range(info.data_offset, info.data_offset + info.data_size):
        pcm_data.append(wav_bytes[i])
    state.playbacks.append(Playback(pcm^, pcm_data^, 0))
    state.log("playing " + filename + " on " + device)


def pump_playback(mut state: AppState):
    if not state.playing():
        return
    try:
        var wrote = state.playbacks[0].pcm.write_some(
            state.playbacks[0].data, state.playbacks[0].offset
        )
        state.playbacks[0].offset += wrote
        if state.playbacks[0].offset >= len(state.playbacks[0].data):
            state.playbacks[0].pcm.drain()
            state.playbacks[0].pcm.close()
            state.playbacks = List[Playback]()
            state.log("playback finished")
    except e:
        state.log("playback error: " + String(e))
        state.playbacks[0].pcm.close()
        state.playbacks = List[Playback]()


def pump_capture(mut state: AppState):
    if not state.capturing() or len(state.ws_clients) == 0:
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
    for fd in state.ws_clients:
        try:
            ws_send(fd, WS_BINARY, chunk)
        except:
            dead.append(fd)
    state.chunks_sent += 1
    for fd in dead:
        state.drop_client(fd)


def set_recv_timeout(fd: c_int, seconds: Int):
    var tv = List[UInt8]()
    for _ in range(16):
        tv.append(0)
    tv[0] = UInt8(seconds & 0xFF)
    _ = external_call["setsockopt", c_int](
        fd, c_int(1), c_int(20), tv.unsafe_ptr(), c_int(16)
    )


def handle_http(mut state: AppState, conn: c_int) raises -> Bool:
    # Returns True when the connection was upgraded to a WebSocket.
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
                conn, content_type_for(req.path), read_file_bytes("/app" + req.path)
            )
        except:
            respond(conn, "404 Not Found", "text/plain", "not found")
    elif req.path == "/microphones":
        respond_json(conn, "200 OK", devices_json(True))
    elif req.path == "/speakers":
        respond_json(conn, "200 OK", devices_json(False))
    elif req.path == "/sounds":
        respond_json(conn, "200 OK", sounds_json())
    elif req.method == "POST" and req.path.startswith("/play/"):
        var filename = String(req.path[byte=6:])
        if filename.endswith(".wav") and filename.find("..") < 0 and filename.find("/") < 0:
            try:
                start_playback(state, filename)
                respond_json(
                    conn,
                    "200 OK",
                    '{"status":"playing","file":"' + json_escape(filename) + '"}',
                )
            except e:
                respond_json(
                    conn, "500 Internal Server Error", '{"error":"' + json_escape(String(e)) + '"}'
                )
        else:
            respond_json(conn, "404 Not Found", '{"error":"not found"}')
    elif req.method == "POST" and req.path.startswith("/speaker/"):
        state.current_speaker = String(req.path[byte=9:])
        state.log("speaker set to " + state.current_speaker)
        respond_json(
            conn,
            "200 OK",
            '{"status":"ok","speaker":"' + json_escape(state.current_speaker) + '"}',
        )
    elif req.path == "/logs":
        var out = String("[")
        for i in range(len(state.logs)):
            if i > 0:
                out += ","
            out += '"' + json_escape(state.logs[i]) + '"'
        respond_json(conn, "200 OK", out + "]")
    elif req.path == "/debug":
        respond_json(
            conn,
            "200 OK",
            '{"mode":"pcm-s16le-ws","capturing":'
            + ("true" if state.capturing() else "false")
            + ',"num_clients":'
            + String(len(state.ws_clients))
            + ',"chunks_sent":'
            + String(state.chunks_sent)
            + ',"microphones":'
            + devices_json(True)
            + ',"speakers":'
            + devices_json(False)
            + ',"sounds":'
            + sounds_json()
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
            var target = json_find_string(text, "switch_microphone")
            if target != "":
                state.log("switching microphone to " + target)
                state.close_capture()
                state.preferred_mic = target
                var ack: String
                if state.try_open_capture():
                    ack = (
                        '{"type":"mic_switched","device":"'
                        + json_escape(target)
                        + '"}'
                    )
                else:
                    ack = '{"type":"mic_switch_failed"}'
                ws_send(fd, WS_TEXT, str_bytes(ack))
        return True
    except:
        return False


def main() raises:
    var port = DEFAULT_PORT
    var port_env = getenv("PORT")
    if port_env != "":
        port = Int(port_env)
    var state = AppState()
    var listener = Listener(port)
    state.log("audio (mojo) listening on :" + String(port))

    while True:
        var ps = PollSet()
        ps.add(listener.fd, POLLIN)
        for fd in state.ws_clients:
            ps.add(fd, POLLIN)

        # Audio devices are drained on a timer tick rather than fd readiness:
        # 50 ms keeps capture latency under one chunk without ALSA poll-fd
        # plumbing; idle server polls at 1 s.
        var timeout = 50 if (state.capturing() or state.playing()) else 1000
        var ready = ps.poll(timeout)

        if len(state.ws_clients) > 0 and not state.capturing():
            state.ticks_since_retry += 1
            if state.ticks_since_retry >= 10 or timeout == 1000:
                state.ticks_since_retry = 0
                _ = state.try_open_capture()

        pump_capture(state)
        pump_playback(state)

        if ready == 0:
            continue

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
            elif not state.capturing():
                _ = state.try_open_capture()

        for i in range(len(clients)):
            var revents = ps.revents(1 + i)
            if (revents & (POLLERR | POLLHUP)) != 0:
                state.drop_client(clients[i])
            elif (revents & POLLIN) != 0:
                if not handle_ws_message(state, clients[i]):
                    state.drop_client(clients[i])
