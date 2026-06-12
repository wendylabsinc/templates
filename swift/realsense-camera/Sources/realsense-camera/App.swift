internal import Foundation
import Hummingbird
import Logging
import NIOCore
import OTel
import ServiceLifecycle

struct RunningStatus: Encodable {
    let running: Bool
}

struct ConfigResponse: Encodable {
    let width: Int
    let height: Int
    let fps: Int
    let preset: String
}

struct ErrorResponse: Encodable {
    let error: String
}

private enum ParamError: Error {
    case invalid(String)
}

private func jsonResponse(_ value: some Encodable, status: HTTPResponse.Status = .ok) throws -> Response {
    let data = try JSONEncoder().encode(value)
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    return Response(
        status: status,
        headers: [.contentType: "application/json"],
        body: .init(byteBuffer: buffer)
    )
}

private func intParam(_ request: Request, _ name: String, fallback: Int, min: Int, max: Int) throws -> Int {
    guard let raw = request.uri.queryParameters.get(name).map(String.init), !raw.isEmpty else {
        return fallback
    }
    guard let value = Int(raw) else {
        throw ParamError.invalid("\(name) must be an integer")
    }
    guard (min...max).contains(value) else {
        throw ParamError.invalid("\(name) must be between \(min) and \(max)")
    }
    return value
}

@main
struct App {
    static func main() async throws {
        let observability = try OTel.bootstrap()
        let logger = Logger(label: "{{.APP_ID}}")

        let store = FrameStore()
        let pump = RealSensePump(store: store, logger: logger)

        let router = Router()
        router.middlewares.add(TracingMiddleware())
        router.middlewares.add(MetricsMiddleware())

        router.post("/start") { _, _ -> Response in
            await pump.start()
            return try jsonResponse(RunningStatus(running: await store.running))
        }

        router.post("/stop") { _, _ -> Response in
            await pump.stop()
            return try jsonResponse(RunningStatus(running: await store.running))
        }

        router.post("/config") { request, _ -> Response in
            do {
                let width = try intParam(request, "width", fallback: 640, min: 1, max: 8192)
                let height = try intParam(request, "height", fallback: 480, min: 1, max: 8192)
                let fps = try intParam(request, "fps", fallback: 30, min: 1, max: 300)
                let preset = request.uri.queryParameters.get("preset").map(String.init) ?? "default"
                guard PumpConfig.presets.contains(preset) else {
                    return try jsonResponse(ErrorResponse(error: "Unknown preset: \(preset)"), status: .badRequest)
                }
                await pump.configure(width: width, height: height, fps: fps, preset: preset)
                return try jsonResponse(ConfigResponse(width: width, height: height, fps: fps, preset: preset))
            } catch ParamError.invalid(let message) {
                return try jsonResponse(ErrorResponse(error: message), status: .badRequest)
            }
        }

        router.get("/health") { _, _ -> Response in
            try jsonResponse(await store.health())
        }

        router.get("/stream/{streamId}") { _, context -> Response in
            let streamId = try context.parameters.require("streamId")
            guard FrameStore.streamIds.contains(streamId) else {
                return try jsonResponse(ErrorResponse(error: "Unknown stream: \(streamId)"), status: .notFound)
            }

            var headers = HTTPFields()
            headers[.contentType] = "multipart/x-mixed-replace; boundary=frame"
            headers[.cacheControl] = "no-store"
            return Response(status: .ok, headers: headers, body: .init { writer in
                var lastSequence: UInt64 = 0
                while let frame = await store.waitForFrame(
                    stream: streamId, after: lastSequence, timeout: .seconds(5)
                ) {
                    lastSequence = frame.sequence
                    var part = ByteBuffer()
                    part.writeString("--frame\r\nContent-Type: image/jpeg\r\nContent-Length: \(frame.jpeg.count)\r\n\r\n")
                    part.writeBytes(frame.jpeg)
                    part.writeString("\r\n")
                    try await writer.write(part)
                }
                try await writer.finish(nil)
            })
        }

        router.get("/", use: spaHandler(staticDir: "static"))
        router.get("{path+}", use: spaHandler(staticDir: "static"))

        let app = Application(
            router: router,
            configuration: .init(address: .hostname("0.0.0.0", port: {{.PORT}}))
        )

        let hostname = ProcessInfo.processInfo.environment["WENDY_HOSTNAME"] ?? "0.0.0.0"
        logger.info("Starting server on http://\(hostname):{{.PORT}}")

        try await ServiceGroup(
            services: [observability, app],
            gracefulShutdownSignals: [.sigterm, .sigint],
            logger: logger
        ).run()
    }
}
