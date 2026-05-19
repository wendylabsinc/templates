internal import Foundation
import Logging

actor MJPEGCamera {
    private var subscribers: [ObjectIdentifier: @Sendable (Data) async -> Void] = [:]
    private var pipelineTask: Task<Void, any Error>?
    private var currentDevice: String
    private let logger = Logger(label: "MJPEGCamera")

    init(device: String = "/dev/video0") {
        self.currentDevice = device
    }

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

    func switchCamera(to device: String) {
        guard device != currentDevice else { return }
        currentDevice = device
        if !subscribers.isEmpty {
            stopPipeline()
            startPipeline()
        }
    }

    private func broadcast(_ frame: Data) async {
        for (_, handler) in subscribers {
            await handler(frame)
        }
    }

    private func startPipeline() {
        let device = currentDevice
        pipelineTask = Task {
            do {
                try await self.runGStreamerPipeline(device: device)
            } catch is CancellationError {
                // Normal shutdown
            } catch {
                logger.error("Pipeline error: \(error)")
            }
        }
    }

    private func stopPipeline() {
        pipelineTask?.cancel()
        pipelineTask = nil
    }

    private func runGStreamerPipeline(device: String) async throws {
        let process = Process()
        process.executableURL = URL(filePath: "/usr/bin/gst-launch-1.0")
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
}
