from std.os import getenv

from unitree_mojo import Go2Client
from wendynet.net import Listener, read_request, respond, respond_json


comptime PORT = 3201


def body_string(body: List[UInt8]) raises -> String:
    var bytes = List[UInt8]()
    for byte in body:
        bytes.append(byte)
    return String(from_utf8=bytes)


def json_number(document: String, key: String, default: Float64) -> Float64:
    var offset = document.find('"' + key + '"')
    if offset < 0:
        return default
    var bytes = document.as_bytes()
    var index = offset + key.byte_length() + 2
    while index < len(bytes) and bytes[index] != 58:
        index += 1
    if index >= len(bytes):
        return default
    index += 1
    while index < len(bytes) and (bytes[index] == 32 or bytes[index] == 9):
        index += 1

    var sign = 1.0
    if index < len(bytes) and bytes[index] == 45:
        sign = -1.0
        index += 1
    var value = 0.0
    var found = False
    while index < len(bytes) and bytes[index] >= 48 and bytes[index] <= 57:
        value = value * 10.0 + Float64(bytes[index] - 48)
        found = True
        index += 1
    if index < len(bytes) and bytes[index] == 46:
        index += 1
        var place = 0.1
        while index < len(bytes) and bytes[index] >= 48 and bytes[index] <= 57:
            value += Float64(bytes[index] - 48) * place
            place *= 0.1
            found = True
            index += 1
    return sign * value if found else default


def clamp(value: Float64, limit: Float64) -> Float64:
    return max(-limit, min(limit, value))


def result_json(message: String) -> String:
    return '{"result":"' + message + '"}'


def handle_request(
    connection: Int32,
    request_path: String,
    request_method: String,
    body: List[UInt8],
    ref client: Go2Client,
) raises:
    if request_path == "/health":
        if client.is_ready():
            respond_json(connection, "200 OK", '{"ok":true}')
        else:
            respond_json(
                connection,
                "503 Service Unavailable",
                '{"ok":false,"reason":"sport_client_not_ready"}',
            )
        return
    if request_path == "/state":
        var state = client.state_json()
        respond_json(
            connection,
            "200 OK",
            '{"ok":'
            + ("false" if state == "{}" else "true")
            + ',"state":'
            + state
            + "}",
        )
        return
    if request_path == "/perception":
        var perception = client.perception_json()
        respond_json(
            connection,
            "200 OK",
            perception if perception != "{}" else '{"have_data":false}',
        )
        return
    if request_method != "POST":
        respond_json(
            connection,
            "405 Method Not Allowed",
            '{"error":"method not allowed"}',
        )
        return

    var document = body_string(body)
    if request_path == "/velocity":
        var vx = clamp(json_number(document, "vx", 0), 0.6)
        var vy = clamp(json_number(document, "vy", 0), 0.4)
        var vyaw = clamp(json_number(document, "vyaw", 0), 1.0)
        client.set_velocity(Float32(vx), Float32(vy), Float32(vyaw))
        respond_json(
            connection,
            "200 OK",
            result_json(
                "velocity vx="
                + String(vx)
                + " vy="
                + String(vy)
                + " vyaw="
                + String(vyaw)
            ),
        )
    elif request_path == "/move":
        var vx = clamp(json_number(document, "vx", 0), 0.6)
        var vy = clamp(json_number(document, "vy", 0), 0.4)
        var vyaw = clamp(json_number(document, "vyaw", 0), 1.0)
        var duration = clamp(json_number(document, "duration", 2.0), 10.0)
        duration = max(0.1, duration)
        client.move_for(
            Float32(vx), Float32(vy), Float32(vyaw), UInt32(duration * 1000.0)
        )
        respond_json(
            connection,
            "200 OK",
            result_json(
                "moved vx="
                + String(vx)
                + " vy="
                + String(vy)
                + " vyaw="
                + String(vyaw)
                + " for "
                + String(duration)
                + "s"
            ),
        )
    elif request_path == "/stop":
        client.stop()
        respond_json(connection, "200 OK", result_json("stopped"))
    elif request_path == "/stand":
        client.stand_up()
        respond_json(connection, "200 OK", result_json("standing"))
    elif request_path == "/sit":
        client.sit()
        respond_json(connection, "200 OK", result_json("sitting"))
    elif request_path == "/lie":
        client.stand_down()
        respond_json(connection, "200 OK", result_json("lying down"))
    elif request_path == "/hello":
        client.hello()
        respond_json(connection, "200 OK", result_json("waving"))
    elif request_path == "/dance":
        client.dance()
        respond_json(connection, "200 OK", result_json("dancing"))
    else:
        respond(connection, "404 Not Found", "text/plain", "not found")


def main() raises:
    var interface = getenv("GO2_NETWORK_INTERFACE")
    if interface == "":
        interface = "eth0"
    var client = Go2Client(interface, "/app/lib/libunitree_mojo.so")
    var listener = Listener(PORT)
    print(
        "go2-motion (Mojo 1.0 + Unitree SDK2) listening on :3201 via "
        + interface
    )

    while True:
        var connection: Int32
        try:
            connection = listener.accept()
        except:
            continue
        try:
            var request = read_request(connection)
            handle_request(
                connection, request.path, request.method, request.body, client
            )
        except error:
            print("motion request failed: " + String(error))
            respond_json(
                connection,
                "500 Internal Server Error",
                '{"ok":false,"error":"motion command failed"}',
            )
        listener.close_conn(connection)
