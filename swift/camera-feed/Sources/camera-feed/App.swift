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

@main
struct App {
    static func main() async throws {
        let observability = try OTel.bootstrap()
        let logger = Logger(label: "{{.APP_ID}}")

        let camera = MJPEGCamera(device: "/dev/video0")

        let router = Router()
        router.middlewares.add(TracingMiddleware())
        router.middlewares.add(MetricsMiddleware())

        router.get("/health") { _, _ -> HTTPResponse.Status in .ok }

        router.get("/cameras") { _, _ -> Response in
            let cameras = listCameras()
            let data = try JSONEncoder().encode(cameras)
            var buffer = ByteBuffer()
            buffer.writeBytes(data)
            return Response(
                status: .ok,
                headers: [.contentType: "application/json"],
                body: .init(byteBuffer: buffer)
            )
        }

        let wsRouter = Router(context: BasicWebSocketRequestContext.self)
        wsRouter.ws("/stream") { inbound, outbound, _ in
            final class ConnectionID: Sendable {}
            let connID = ConnectionID()
            let id = ObjectIdentifier(connID)

            await camera.subscribe(id: id) { frame in
                var buffer = ByteBufferAllocator().buffer(capacity: frame.count)
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

        router.get("/", use: spaHandler(staticDir: "."))
        router.get("{path+}", use: spaHandler(staticDir: "."))

        let app = Application(
            router: router,
            server: .http1WebSocketUpgrade(webSocketRouter: wsRouter),
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
