import Foundation
import Hummingbird
import RealSenseBridge

private let boundary = "frame"
private let staticRoot = "static"

private struct RunningResponse: Encodable {
    let running: Bool
}

private struct ConfigResponse: Encodable {
    let width: Int
    let height: Int
    let fps: Int
    let preset: String
}

private struct ErrorResponse: Encodable {
    let error: String
}

private struct Frame: Sendable {
    let data: Data
    let sequence: UInt64
}

private final class RealSenseCamera: @unchecked Sendable {
    private let bridge: RealSenseBridgeRef

    init() throws {
        guard let bridge = RealSenseBridgeCreate() else {
            throw RealSenseError("Failed to initialize RealSense bridge")
        }
        self.bridge = bridge
    }

    deinit {
        RealSenseBridgeDestroy(bridge)
    }

    var isRunning: Bool {
        RealSenseBridgeIsRunning(bridge)
    }

    func start() throws {
        try withBridgeError { error, length in
            RealSenseBridgeStart(bridge, error, length)
        }
    }

    func stop() {
        RealSenseBridgeStop(bridge)
    }

    func configure(width: Int, height: Int, fps: Int, preset: String) throws {
        try preset.withCString { presetCString in
            try withBridgeError { error, length in
                RealSenseBridgeConfigure(bridge, Int32(width), Int32(height), Int32(fps), presetCString, error, length)
            }
        }
    }

    func healthJSON() -> String {
        guard let raw = RealSenseBridgeHealthJSON(bridge) else {
            return #"{"streams":["color","ir-left","ir-right","depth"],"running":false,"fps":{"color":0,"ir-left":0,"ir-right":0,"depth":0}}"#
        }
        defer { RealSenseBridgeFreeString(raw) }
        return String(cString: raw)
    }

    func isKnownStream(_ streamID: String) -> Bool {
        streamID.withCString { RealSenseBridgeIsKnownStream($0) }
    }

    func waitFrame(streamID: String, lastSequence: UInt64, timeoutMilliseconds: Int32) -> Frame? {
        streamID.withCString { streamCString in
            var rawFrame = RealSenseBridgeFrame()
            let ok = RealSenseBridgeWaitFrame(bridge, streamCString, lastSequence, timeoutMilliseconds, &rawFrame)
            guard ok, let bytes = rawFrame.data, rawFrame.length > 0 else {
                return nil
            }
            defer { RealSenseBridgeFreeFrame(&rawFrame) }
            return Frame(data: Data(bytes: bytes, count: rawFrame.length), sequence: rawFrame.sequence)
        }
    }

    private func withBridgeError(_ operation: (UnsafeMutablePointer<CChar>?, Int) -> Bool) throws {
        var error = [CChar](repeating: 0, count: 512)
        let ok = error.withUnsafeMutableBufferPointer { buffer in
            operation(buffer.baseAddress, buffer.count)
        }
        if !ok {
            let message = error.withUnsafeBufferPointer { buffer -> String in
                guard let baseAddress = buffer.baseAddress else { return "" }
                return String(cString: baseAddress)
            }
            throw RealSenseError(message.isEmpty ? "RealSense bridge operation failed" : message)
        }
    }
}

private struct RealSenseError: Error, CustomStringConvertible {
    let description: String

    init(_ description: String) {
        self.description = description
    }
}

private func jsonResponse<Value: Encodable>(_ value: Value, status: HTTPResponse.Status = .ok) throws -> Response {
    let data = try JSONEncoder().encode(value)
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    return Response(
        status: status,
        headers: [.contentType: "application/json"],
        body: .init(byteBuffer: buffer)
    )
}

private func jsonStringResponse(_ json: String) -> Response {
    var buffer = ByteBuffer()
    buffer.writeString(json)
    return Response(
        status: .ok,
        headers: [.contentType: "application/json"],
        body: .init(byteBuffer: buffer)
    )
}

private func jsonError(_ message: String, status: HTTPResponse.Status = .badRequest) throws -> Response {
    try jsonResponse(ErrorResponse(error: message), status: status)
}

private func queryValue(_ request: Request, _ name: String) -> String? {
    request.uri.queryParameters[Substring(name)].map(String.init)
}

private func intQuery(_ request: Request, name: String, fallback: Int, min: Int, max: Int) throws -> Int {
    guard let raw = queryValue(request, name), !raw.isEmpty else {
        return fallback
    }
    guard let value = Int(raw) else {
        throw RealSenseError("\(name) must be an integer")
    }
    guard value >= min && value <= max else {
        throw RealSenseError("\(name) must be between \(min) and \(max)")
    }
    return value
}

private func contentType(for path: String) -> String {
    if path.hasSuffix(".html") { return "text/html; charset=utf-8" }
    if path.hasSuffix(".css") { return "text/css" }
    if path.hasSuffix(".js") { return "application/javascript" }
    if path.hasSuffix(".svg") { return "image/svg+xml" }
    if path.hasSuffix(".png") { return "image/png" }
    if path.hasSuffix(".jpg") || path.hasSuffix(".jpeg") { return "image/jpeg" }
    if path.hasSuffix(".ico") { return "image/x-icon" }
    if path.hasSuffix(".woff2") { return "font/woff2" }
    return "application/octet-stream"
}

private func staticResponse(for requestPath: String) throws -> Response {
    let relativePath: String
    if requestPath == "/" {
        relativePath = "index.html"
    } else {
        relativePath = String(requestPath.dropFirst())
    }

    let safePath = relativePath
        .split(separator: "/")
        .filter { $0 != ".." && !$0.isEmpty }
        .joined(separator: "/")
    let candidate = "\(staticRoot)/\(safePath)"
    let filePath = FileManager.default.fileExists(atPath: candidate) ? candidate : "\(staticRoot)/index.html"

    let data = try Data(contentsOf: URL(fileURLWithPath: filePath))
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    return Response(
        status: .ok,
        headers: [.contentType: contentType(for: filePath)],
        body: .init(byteBuffer: buffer)
    )
}

private func makeMjpegPart(frame: Frame) -> ByteBuffer {
    var buffer = ByteBufferAllocator().buffer(capacity: frame.data.count + 128)
    buffer.writeString("--\(boundary)\r\n")
    buffer.writeString("Content-Type: image/jpeg\r\n")
    buffer.writeString("Content-Length: \(frame.data.count)\r\n\r\n")
    buffer.writeBytes(frame.data)
    buffer.writeString("\r\n")
    return buffer
}

@main
struct RealSenseCameraApp {
    static func main() async throws {
        let hostname = ProcessInfo.processInfo.environment["WENDY_HOSTNAME"] ?? "0.0.0.0"
        let camera = try RealSenseCamera()
        let router = Router()

        router.post("/start") { _, _ -> Response in
            do {
                try camera.start()
                return try jsonResponse(RunningResponse(running: camera.isRunning))
            } catch {
                return try jsonError(String(describing: error), status: .internalServerError)
            }
        }

        router.post("/stop") { _, _ -> Response in
            camera.stop()
            return try jsonResponse(RunningResponse(running: camera.isRunning))
        }

        router.post("/config") { request, _ -> Response in
            do {
                let width = try intQuery(request, name: "width", fallback: 640, min: 1, max: 8192)
                let height = try intQuery(request, name: "height", fallback: 480, min: 1, max: 8192)
                let fps = try intQuery(request, name: "fps", fallback: 30, min: 1, max: 300)
                let preset = queryValue(request, "preset") ?? "default"
                try camera.configure(width: width, height: height, fps: fps, preset: preset)
                return try jsonResponse(ConfigResponse(width: width, height: height, fps: fps, preset: preset))
            } catch {
                return try jsonError(String(describing: error))
            }
        }

        router.get("/health") { _, _ -> Response in
            jsonStringResponse(camera.healthJSON())
        }

        router.get("/stream/:streamID") { _, context -> Response in
            guard let streamID = context.parameters.get("streamID"), camera.isKnownStream(streamID) else {
                return try jsonError("Unknown stream", status: .notFound)
            }

            let body = ResponseBody { writer in
                var lastSequence: UInt64 = 0
                while !Task.isCancelled {
                    let requestedSequence = lastSequence
                    let frame = await Task.detached(priority: .userInitiated) {
                        camera.waitFrame(streamID: streamID, lastSequence: requestedSequence, timeoutMilliseconds: 5000)
                    }.value

                    if let frame {
                        lastSequence = frame.sequence
                        try await writer.write(makeMjpegPart(frame: frame))
                    } else {
                        try? await Task.sleep(for: .milliseconds(100))
                    }
                }
                try await writer.finish(nil)
            }

            return Response(
                status: .ok,
                headers: [
                    .contentType: "multipart/x-mixed-replace; boundary=\(boundary)",
                    .cacheControl: "no-store",
                ],
                body: body
            )
        }

        router.get("/") { request, _ -> Response in
            try staticResponse(for: request.uri.path)
        }

        router.get("/**") { request, _ -> Response in
            try staticResponse(for: request.uri.path)
        }

        let app = Application(
            router: router,
            configuration: .init(address: .hostname("0.0.0.0", port: {{.PORT}}))
        )

        print("RealSense Swift server running on http://\(hostname):{{.PORT}}")
        try await app.runService()
        camera.stop()
    }
}
