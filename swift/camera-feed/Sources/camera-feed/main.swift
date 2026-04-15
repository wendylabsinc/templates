import Foundation
import Hummingbird
import HummingbirdWebSocket
import GStreamer

// MARK: - JPEG Frame Parser

/// Extracts complete JPEG frames from a byte stream using SOI (FFD8) and EOI (FFD9) markers.
struct JPEGFrameParser: Sendable {
    private var buffer = Data()

    mutating func append(_ data: Data) -> [Data] {
        buffer.append(data)
        var frames: [Data] = []

        while let range = findFrame() {
            frames.append(Data(buffer[range]))
            buffer.removeSubrange(buffer.startIndex...range.upperBound)
        }

        // Prevent unbounded growth if no valid frames arrive
        if buffer.count > 10_000_000 {
            buffer.removeAll()
        }

        return frames
    }

    private func findFrame() -> ClosedRange<Int>? {
        guard buffer.count >= 4 else { return nil }

        var soi: Int?
        for i in buffer.startIndex..<(buffer.endIndex - 1) {
            if buffer[i] == 0xFF && buffer[i + 1] == 0xD8 {
                soi = i
                break
            }
        }
        guard let start = soi else { return nil }

        for i in (start + 2)..<(buffer.endIndex - 1) {
            if buffer[i] == 0xFF && buffer[i + 1] == 0xD9 {
                return start...(i + 1)
            }
        }
        return nil
    }
}

// MARK: - Camera Info

struct CameraInfo: Codable, Sendable {
    let id: String
    let name: String
}

// MARK: - MJPEGCamera Actor

/// Manages a GStreamer pipeline singleton, tracks WebSocket clients, and broadcasts
/// JPEG frames. Starts the pipeline on first subscriber and stops when the last
/// subscriber disconnects. Supports switching cameras at runtime.
actor MJPEGCamera {
    private var subscribers: [ObjectIdentifier: @Sendable (Data) async -> Void] = [:]
    private var pipelineTask: Task<Void, any Error>?
    private var currentDevice: String

    init(device: String = "/dev/video0") {
        self.currentDevice = device
    }

    // MARK: Subscribe / Unsubscribe

    func subscribe(id: ObjectIdentifier, handler: @escaping @Sendable (Data) async -> Void) {
        subscribers[id] = handler
        if subscribers.count == 1 {
            startPipeline()
        }
    }

    func unsubscribe(id: ObjectIdentifier) {
        subscribers.removeValue(forKey: id)
        if subscribers.isEmpty {
            stopPipeline()
        }
    }

    var subscriberCount: Int { subscribers.count }

    // MARK: Camera Switching

    func switchCamera(to device: String) {
        guard device != currentDevice else { return }
        currentDevice = device
        if !subscribers.isEmpty {
            stopPipeline()
            startPipeline()
        }
    }

    // MARK: Broadcast

    private func broadcast(_ frame: Data) async {
        for (_, handler) in subscribers {
            await handler(frame)
        }
    }

    // MARK: Pipeline Lifecycle

    private func startPipeline() {
        let device = currentDevice
        pipelineTask = Task { [weak self] in
            guard let self else { return }
            do {
                try await self.runGStreamerPipeline(device: device)
            } catch is CancellationError {
                // Normal shutdown
            } catch {
                print("Pipeline error: \(error)")
            }
        }
    }

    private func stopPipeline() {
        pipelineTask?.cancel()
        pipelineTask = nil
    }

    // MARK: GStreamer Pipeline (via gst-launch-1.0 subprocess)

    /// Spawns gst-launch-1.0 to capture MJPEG from a V4L2 device, writing raw
    /// JPEG frames to stdout via fdsink. We parse SOI/EOI markers to extract
    /// individual frames and broadcast them to all connected WebSocket clients.
    private func runGStreamerPipeline(device: String) async throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/gst-launch-1.0")
        process.arguments = [
            "v4l2src", "device=\(device)", "!",
            "image/jpeg", "!",
            "jpegdec", "!",
            "jpegenc", "quality=85", "!",
            "fdsink", "fd=1",
        ]

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice

        try process.run()

        let handle = pipe.fileHandleForReading
        var parser = JPEGFrameParser()

        await withTaskCancellationHandler {
            while !Task.isCancelled {
                let chunk = handle.availableData
                if chunk.isEmpty { break }

                let frames = parser.append(chunk)
                for frame in frames {
                    await self.broadcast(frame)
                }
            }
            process.terminate()
        } onCancel: {
            process.terminate()
        }
    }

    // MARK: Camera Discovery

    /// Lists available cameras. Tries GStreamer DeviceMonitor first, falls back
    /// to scanning /dev/video* entries.
    static func listCameras() -> [CameraInfo] {
        // Try v4l2-ctl first for structured output
        let cameras = listCamerasViaV4L2()
        if !cameras.isEmpty { return cameras }

        // Fallback: scan /dev/video* devices
        return listCamerasViaScan()
    }

    private static func listCamerasViaV4L2() -> [CameraInfo] {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/v4l2-ctl")
        process.arguments = ["--list-devices"]

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice

        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return []
        }

        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard let output = String(data: data, encoding: .utf8) else { return [] }

        var cameras: [CameraInfo] = []
        var currentName: String?

        for line in output.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if !line.hasPrefix("\t") && !line.hasPrefix(" ") && trimmed.hasSuffix(":") {
                currentName = String(trimmed.dropLast())
            } else if trimmed.hasPrefix("/dev/video") {
                cameras.append(CameraInfo(
                    id: trimmed,
                    name: currentName ?? trimmed
                ))
            }
        }

        return cameras
    }

    private static func listCamerasViaScan() -> [CameraInfo] {
        let fm = FileManager.default
        var cameras: [CameraInfo] = []

        for i in 0..<16 {
            let path = "/dev/video\(i)"
            if fm.fileExists(atPath: path) {
                cameras.append(CameraInfo(id: path, name: "Camera \(i)"))
            }
        }

        return cameras
    }
}

// MARK: - Switch Camera Message

private struct SwitchCameraMessage: Decodable {
    let switch_camera: String
}

// MARK: - Application

@main
struct CameraFeedApp {
    static func main() async throws {
        let hostname = ProcessInfo.processInfo.environment["WENDY_HOSTNAME"] ?? "0.0.0.0"
        let camera = MJPEGCamera(device: "/dev/video0")

        let router = Router()

        // Serve index.html at root
        router.get("/") { _, _ -> Response in
            let indexPath = "index.html"
            if FileManager.default.fileExists(atPath: indexPath) {
                let data = try Data(contentsOf: URL(fileURLWithPath: indexPath))
                return Response(
                    status: .ok,
                    headers: [.contentType: "text/html; charset=utf-8"],
                    body: .init(byteBuffer: .init(bytes: data))
                )
            }
            return Response(status: .notFound, body: .init(byteBuffer: .init(string: "index.html not found")))
        }

        // Serve static assets
        router.get("/assets/*") { request, _ -> Response in
            let path = request.uri.path
            let filePath = String(path.dropFirst()) // remove leading /
            if FileManager.default.fileExists(atPath: filePath) {
                let data = try Data(contentsOf: URL(fileURLWithPath: filePath))
                var contentType = "application/octet-stream"
                if filePath.hasSuffix(".svg") {
                    contentType = "image/svg+xml"
                } else if filePath.hasSuffix(".png") {
                    contentType = "image/png"
                } else if filePath.hasSuffix(".jpg") || filePath.hasSuffix(".jpeg") {
                    contentType = "image/jpeg"
                } else if filePath.hasSuffix(".css") {
                    contentType = "text/css"
                } else if filePath.hasSuffix(".js") {
                    contentType = "application/javascript"
                } else if filePath.hasSuffix(".wav") {
                    contentType = "audio/wav"
                } else if filePath.hasSuffix(".mp3") {
                    contentType = "audio/mpeg"
                }
                return Response(
                    status: .ok,
                    headers: [.contentType: contentType],
                    body: .init(byteBuffer: .init(bytes: data))
                )
            }
            return Response(status: .notFound)
        }

        // List available cameras as JSON
        router.get("/cameras") { _, _ -> Response in
            let cameras = MJPEGCamera.listCameras()
            let data = try JSONEncoder().encode(cameras)
            return Response(
                status: .ok,
                headers: [.contentType: "application/json"],
                body: .init(byteBuffer: .init(bytes: data))
            )
        }

        // WebSocket /stream — sends binary JPEG frames, accepts camera switch commands
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
                if case .text(let text) = message {
                    if let data = text.data(using: .utf8),
                       let cmd = try? JSONDecoder().decode(SwitchCameraMessage.self, from: data)
                    {
                        await camera.switchCamera(to: cmd.switch_camera)
                    }
                }
            }

            await camera.unsubscribe(id: id)
        }

        let app = Application(
            router: router,
            server: .http1WebSocketUpgrade(webSocketRouter: wsRouter),
            configuration: .init(address: .hostname("0.0.0.0", port: {{.PORT}}))
        )

        print("Camera feed running on http://\(hostname):{{.PORT}}")
        try await app.runService()
    }
}
