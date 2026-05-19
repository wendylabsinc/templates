internal import Foundation
import Hummingbird
import HummingbirdWebSocket
import Logging
import NIOCore
import OTel
import ServiceLifecycle

private struct SwitchCameraMessage: Decodable {
    let switch_camera: String
}

private struct SwitchMicrophoneMessage: Decodable {
    let switch_microphone: String
}

@main
struct App {
    static func main() async throws {
        let observability = try OTel.bootstrap()
        let logger = Logger(label: "{{.APP_ID}}")

        let carStore: CarStore
        do {
            carStore = try CarStore(path: "/data/cars.db")
        } catch {
            logger.critical("Failed to initialize database: \(error)")
            throw error
        }

        let camera = MJPEGCamera(device: "/dev/video0")
        let audio = AudioCapture()
        let staticDir = "./static"

        // MARK: - HTTP Router

        let router = Router()
        router.middlewares.add(TracingMiddleware())
        router.middlewares.add(MetricsMiddleware())

        router.get("/health") { _, _ -> HTTPResponse.Status in .ok }

        // MARK: Cars CRUD

        let carsGroup = router.group("api/cars")

        carsGroup.get { _, _ -> Response in
            try jsonResponse(await carStore.all(), status: .ok)
        }

        carsGroup.post { request, context -> Response in
            let input = try await request.decode(as: CarInput.self, context: context)
            guard let car = try await carStore.create(input: input) else {
                throw HTTPError(.internalServerError, message: "Failed to create car")
            }
            return try jsonResponse(car, status: .created)
        }

        carsGroup.get(":id") { _, context -> Response in
            guard let id = context.parameters.get("id", as: Int.self) else {
                throw HTTPError(.badRequest, message: "Invalid car ID")
            }
            guard let car = try await carStore.get(id: id) else {
                throw HTTPError(.notFound, message: "Car not found")
            }
            return try jsonResponse(car, status: .ok)
        }

        carsGroup.put(":id") { request, context -> Response in
            guard let id = context.parameters.get("id", as: Int.self) else {
                throw HTTPError(.badRequest, message: "Invalid car ID")
            }
            let input = try await request.decode(as: CarInput.self, context: context)
            guard let car = try await carStore.update(id: id, input: input) else {
                throw HTTPError(.notFound, message: "Car not found")
            }
            return try jsonResponse(car, status: .ok)
        }

        carsGroup.delete(":id") { _, context -> HTTPResponse.Status in
            guard let id = context.parameters.get("id", as: Int.self) else {
                throw HTTPError(.badRequest, message: "Invalid car ID")
            }
            guard try await carStore.delete(id: id) else {
                throw HTTPError(.notFound, message: "Car not found")
            }
            return .noContent
        }

        // MARK: Device Endpoints

        router.get("api/cameras") { _, _ in try jsonResponse(listCameras(), status: .ok) }
        router.get("api/microphones") { _, _ in try jsonResponse(listAlsaDevices(command: "arecord -l"), status: .ok) }
        router.get("api/speakers") { _, _ in try jsonResponse(listAlsaDevices(command: "aplay -l"), status: .ok) }
        router.get("api/gpu") { _, _ in try jsonResponse(gpuInfo(), status: .ok) }
        router.get("api/system") { _, _ in try jsonResponse(systemInfo(), status: .ok) }

        router.get("{path+}", use: spaHandler(staticDir: staticDir))

        // MARK: - WebSocket Router

        let wsRouter = Router(context: BasicWebSocketRequestContext.self)

        wsRouter.ws("api/camera/stream") { inbound, outbound, _ in
            final class ConnectionID: Sendable {}
            let connID = ConnectionID()
            let id = ObjectIdentifier(connID)

            await camera.subscribe(id: id) { frame in
                var buffer = ByteBuffer()
                buffer.writeBytes(frame)
                try? await outbound.write(.binary(buffer))
            }

            for try await message in inbound.messages(maxSize: 1_048_576) {
                if case .text(let text) = message,
                   let data = text.data(using: .utf8),
                   let cmd = try? JSONDecoder().decode(SwitchCameraMessage.self, from: data)
                {
                    await camera.switchCamera(to: cmd.switch_camera)
                }
            }

            await camera.unsubscribe(id: id)
            _ = connID
        }

        wsRouter.ws("api/audio/stream") { inbound, outbound, _ in
            final class ConnectionID: Sendable {}
            let connID = ConnectionID()
            let id = ObjectIdentifier(connID)

            await audio.subscribe(id: id) { chunk in
                var buffer = ByteBufferAllocator().buffer(capacity: chunk.count)
                buffer.writeBytes(chunk)
                try? await outbound.write(.binary(buffer))
            }

            for try await message in inbound.messages(maxSize: 1_048_576) {
                if case .text(let text) = message,
                   let data = text.data(using: .utf8),
                   let cmd = try? JSONDecoder().decode(SwitchMicrophoneMessage.self, from: data)
                {
                    await audio.switchMicrophone(to: cmd.switch_microphone)
                }
            }

            await audio.unsubscribe(id: id)
            _ = connID
        }

        // MARK: - Start

        let app = Application(
            router: router,
            server: .http1WebSocketUpgrade(webSocketRouter: wsRouter),
            configuration: .init(
                address: .hostname("0.0.0.0", port: {{.PORT}})
            )
        )

        let hostDisplay = ProcessInfo.processInfo.environment["WENDY_HOSTNAME"] ?? "0.0.0.0"
        logger.info("Starting server on http://\(hostDisplay):{{.PORT}}")

        let serviceGroup = ServiceGroup(
            services: [observability, app],
            gracefulShutdownSignals: [.sigterm, .sigint],
            logger: logger
        )
        try await serviceGroup.run()
    }
}

private func jsonResponse<T: Encodable>(_ value: T, status: HTTPResponse.Status) throws -> Response {
    let data = try JSONEncoder().encode(value)
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    return Response(
        status: status,
        headers: [.contentType: "application/json"],
        body: .init(byteBuffer: buffer)
    )
}
