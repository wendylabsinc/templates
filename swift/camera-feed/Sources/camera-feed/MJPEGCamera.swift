internal import Foundation
import Logging

actor MJPEGCamera {
    private var subscribers: [UUID: @Sendable (Data) async -> Void] = [:]
    private var pipelineTask: Task<Void, any Error>?
    private var currentDevice: String
    private let logger = Logger(label: "MJPEGCamera")

    init(device: String = "/dev/video0") {
        self.currentDevice = device
    }

    func subscribe(id: UUID, handler: @escaping @Sendable (Data) async -> Void) {
        subscribers[id] = handler
        if subscribers.count == 1 {
            startPipeline()
        }
    }

    func unsubscribe(id: UUID) {
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
        let handlers = Array(subscribers.values)
        await withTaskGroup(of: Void.self) { group in
            for handler in handlers {
                group.addTask { await handler(frame) }
            }
        }
    }

    private func startPipeline() {
        let device = currentDevice
        pipelineTask = Task {
            var delayMs: UInt64 = 1000
            while !Task.isCancelled {
                guard !subscribers.isEmpty else { return }
                do {
                    try await self.runGStreamerPipeline(device: device)
                } catch is CancellationError {
                    return
                } catch {
                    logger.error("Pipeline error: \(error)")
                }
                guard !Task.isCancelled else { return }
                guard !subscribers.isEmpty else { return }
                logger.warning("Retrying pipeline in \(delayMs)ms")
                do {
                    try await Task.sleep(for: .milliseconds(delayMs))
                } catch {
                    return
                }
                delayMs = min(UInt64(Double(delayMs) * 1.5), 5000)
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
        let (stream, continuation) = AsyncStream<Data>.makeStream()
        handle.readabilityHandler = { fh in
            let data = fh.availableData
            if data.isEmpty { continuation.finish() } else { continuation.yield(data) }
        }
        defer { handle.readabilityHandler = nil }

        var parser = JPEGFrameParser()
        await withTaskCancellationHandler {
            for await chunk in stream {
                let frames = parser.append(chunk)
                for frame in frames { await self.broadcast(frame) }
            }
            process.terminate()
        } onCancel: {
            process.terminate()
            continuation.finish()
        }
    }
}
