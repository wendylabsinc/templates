import Foundation
import Hummingbird
import HummingbirdWebSocket

// MARK: - JPEG Frame Parser

/// Extracts complete JPEG frames from a byte stream using SOI (FFD8) and EOI (FFD9) markers.
struct JPEGFrameParser {
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

// MARK: - Frame Broadcaster

/// Actor that manages WebSocket subscribers and broadcasts JPEG frames.
actor FrameBroadcaster {
    private var subscribers: [ObjectIdentifier: @Sendable (Data) async -> Void] = [:]

    func subscribe(id: ObjectIdentifier, handler: @escaping @Sendable (Data) async -> Void) {
        subscribers[id] = handler
    }

    func unsubscribe(id: ObjectIdentifier) {
        subscribers.removeValue(forKey: id)
    }

    func broadcast(_ frame: Data) async {
        for (_, handler) in subscribers {
            await handler(frame)
        }
    }

    var subscriberCount: Int {
        subscribers.count
    }
}

// MARK: - GStreamer Pipeline

/// Spawns a GStreamer pipeline that captures MJPEG from a V4L2 device and writes to stdout.
func startGStreamerPipeline(device: String, broadcaster: FrameBroadcaster) async throws {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/gst-launch-1.0")
    process.arguments = [
        "v4l2src", "device=\(device)", "!",
        "image/jpeg", "!",
        "fdsink", "fd=1"
    ]

    let pipe = Pipe()
    process.standardOutput = pipe
    process.standardError = FileHandle.nullDevice

    try process.run()

    let handle = pipe.fileHandleForReading
    var parser = JPEGFrameParser()

    // Read in a background task
    await withTaskCancellationHandler {
        while !Task.isCancelled {
            let chunk = handle.availableData
            if chunk.isEmpty { break }

            let frames = parser.append(chunk)
            for frame in frames {
                await broadcaster.broadcast(frame)
            }
        }
        process.terminate()
    } onCancel: {
        process.terminate()
    }
}

// MARK: - Camera Discovery

struct CameraInfo: Codable, Sendable {
    let id: String
    let name: String
}

func listCameras() -> [CameraInfo] {
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
            // Device name line, e.g. "HD Webcam (usb-0000:00:14.0-1):"
            currentName = String(trimmed.dropLast())
        } else if trimmed.hasPrefix("/dev/video") {
            let devicePath = trimmed
            cameras.append(CameraInfo(
                id: devicePath,
                name: currentName ?? devicePath
            ))
        }
    }

    return cameras
}

// MARK: - Application

@main
struct CameraFeedApp {
    static func main() async throws {
        let hostname = ProcessInfo.processInfo.environment["WENDY_HOSTNAME"] ?? "0.0.0.0"
        let broadcaster = FrameBroadcaster()

        // Start the GStreamer pipeline in the background
        let pipelineTask = Task {
            try await startGStreamerPipeline(device: "/dev/video0", broadcaster: broadcaster)
        }

        let router = Router()

        // Serve the built-in index.html
        router.get("/") { request, context -> Response in
            let indexPath = "index.html"
            if FileManager.default.fileExists(atPath: indexPath) {
                let data = try Data(contentsOf: URL(fileURLWithPath: indexPath))
                return Response(
                    status: .ok,
                    headers: [.contentType: "text/html; charset=utf-8"],
                    body: .init(byteBuffer: .init(data: data))
                )
            }
            return Response(status: .notFound, body: .init(byteBuffer: .init(string: "index.html not found")))
        }

        // List available cameras as JSON
        router.get("/cameras") { _, _ -> Response in
            let cameras = listCameras()
            let data = try JSONEncoder().encode(cameras)
            return Response(
                status: .ok,
                headers: [.contentType: "application/json"],
                body: .init(byteBuffer: .init(data: data))
            )
        }

        // WebSocket upgrade for streaming JPEG frames
        router.ws("/stream") { inbound, outbound, _ in
            // Use a unique identifier for this connection
            final class ConnectionID: Sendable {}
            let connID = ConnectionID()
            let id = ObjectIdentifier(connID)

            await broadcaster.subscribe(id: id) { frame in
                try? await outbound.write(.binary(frame))
            }

            // Keep connection alive by consuming inbound messages
            for try await _ in inbound {}

            await broadcaster.unsubscribe(id: id)
        }

        let app = Application(
            router: router,
            configuration: .init(address: .hostname("0.0.0.0", port: {{.PORT}}))
        )

        print("Camera feed running on http://\(hostname):{{.PORT}}")
        try await app.runService()

        pipelineTask.cancel()
    }
}
