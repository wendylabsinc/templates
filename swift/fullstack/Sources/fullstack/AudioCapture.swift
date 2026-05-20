internal import Foundation
import Logging

actor AudioCapture {
    private var subscribers: [UUID: @Sendable (Data) async -> Void] = [:]
    private var pipelineTask: Task<Void, any Error>?
    private var currentDevice: String?
    private let logger = Logger(label: "AudioCapture")

    func subscribe(id: UUID, handler: @escaping @Sendable (Data) async -> Void) {
        subscribers[id] = handler
        if subscribers.count == 1 { startPipeline() }
    }

    func unsubscribe(id: UUID) {
        subscribers.removeValue(forKey: id)
        if subscribers.isEmpty { stopPipeline() }
    }

    func switchMicrophone(to device: String) {
        currentDevice = device
        if !subscribers.isEmpty {
            stopPipeline()
            startPipeline()
        }
    }

    private func broadcast(_ chunk: Data) async {
        for (_, handler) in subscribers { await handler(chunk) }
    }

    private func startPipeline() {
        let device = currentDevice
        pipelineTask = Task {
            do {
                try await self.runPipeline(device: device)
            } catch is CancellationError {
                // normal shutdown
            } catch {
                logger.error("pipeline error: \(error)")
            }
        }
    }

    private func stopPipeline() {
        pipelineTask?.cancel()
        pipelineTask = nil
    }

    private func runPipeline(device: String?) async throws {
        let process = Process()
        process.executableURL = URL(filePath: "/usr/bin/gst-launch-1.0")
        if let device {
            process.arguments = [
                "alsasrc", "device=\(device)", "!",
                "audioconvert", "!",
                "audioresample", "!",
                "audio/x-raw,format=S16LE,channels=1,rate=16000", "!",
                "fdsink", "fd=1",
            ]
        } else {
            process.arguments = [
                "autoaudiosrc", "!",
                "audioconvert", "!",
                "audioresample", "!",
                "audio/x-raw,format=S16LE,channels=1,rate=16000", "!",
                "fdsink", "fd=1",
            ]
        }
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

        await withTaskCancellationHandler {
            for await chunk in stream { await self.broadcast(chunk) }
            process.terminate()
        } onCancel: {
            process.terminate()
            continuation.finish()
        }
    }
}
