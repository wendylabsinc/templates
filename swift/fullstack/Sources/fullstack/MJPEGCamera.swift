internal import Foundation
#if os(Linux)
import GStreamer
#endif
import Logging

#if os(Linux)
private struct CameraProfile: Sendable {
    let name: String
    let resolution: VideoSource.Resolution?
    let framerate: Int
    let quality: Int
}
#endif

private enum CameraError: Error, CustomStringConvertible {
    case unsupportedPlatform

    var description: String {
        switch self {
        case .unsupportedPlatform:
            "camera streaming is only supported on Linux"
        }
    }
}

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
        if subscribers.count == 1 { startPipeline() }
    }

    func unsubscribe(id: UUID) {
        subscribers.removeValue(forKey: id)
        if subscribers.isEmpty { stopPipeline() }
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

    private func runPipeline(device: String) async throws {
        while !Task.isCancelled {
            do {
                try await streamCamera(device: device)
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                logger.warning("camera pipeline failed; retrying", metadata: ["device": "\(device)", "error": "\(error)"])
            }

            try Task.checkCancellation()
            try await Task.sleep(for: .seconds(1))
        }
    }

    #if os(Linux)
    private func cameraProfiles() -> [CameraProfile] {
        [
            CameraProfile(name: "vga-30", resolution: .vga, framerate: 30, quality: 80),
            CameraProfile(name: "hd720-30", resolution: .hd720p, framerate: 30, quality: 80),
            CameraProfile(name: "default-30", resolution: nil, framerate: 30, quality: 80),
        ]
    }

    private func makeVideoSource(device: String) throws -> (String, VideoSource) {
        var errors: [String] = []

        for profile in cameraProfiles() {
            do {
                var builder = try VideoSource.webcam(devicePath: device)
                    .withFramerate(profile.framerate)
                    .withJPEGEncoding(quality: profile.quality)

                if let resolution = profile.resolution {
                    builder = builder.withResolution(resolution)
                }

                let source = try builder.build()
                logger.info(
                    "selected camera pipeline",
                    metadata: ["device": "\(device)", "profile": "\(profile.name)", "pipeline": "\(source.selectedPipeline)"]
                )
                return (profile.name, source)
            } catch {
                errors.append("\(profile.name): \(error)")
            }
        }

        throw VideoSource.VideoSourceError.noWorkingPipeline(errors)
    }
    #endif

    private func streamCamera(device: String) async throws {
        #if os(Linux)
        let (profile, source) = try makeVideoSource(device: device)
        logger.info("started camera pipeline", metadata: ["device": "\(device)", "profile": "\(profile)"])

        do {
            for try await frame in source.frames() {
                try Task.checkCancellation()
                let data = try frame.withUnsafeBytes { buffer in
                    Data(buffer)
                }
                await broadcast(data)
            }
            await source.stop()
        } catch {
            await source.stop()
            throw error
        }

        logger.warning("camera pipeline exited", metadata: ["device": "\(device)", "profile": "\(profile)"])
        #else
        throw CameraError.unsupportedPlatform
        #endif
    }
}
