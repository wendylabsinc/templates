internal import Foundation
import CTurboJPEG
import Logging
import RealSenseKit
import Synchronization

struct PumpConfig: Sendable, Equatable {
    var width = 640
    var height = 480
    var fps = 30
    var preset = "default"

    static let presets = ["default", "hand", "high-accuracy", "high-density", "medium-density"]
}

// State shared between the pump actor and the capture thread. The thread
// polls these between frames; frame data flows through an AsyncStream so
// publish order is preserved.
private final class WorkerShared: Sendable {
    let stopRequested = Atomic<Bool>(false)
    let pendingPreset = Mutex<String?>(nil)
}

// Owns the capture worker lifecycle: a dedicated Thread runs the blocking
// librealsense loop (never the cooperative pool), encodes frames with
// TurboJPEG, and yields batches into an AsyncStream consumed by a Task that
// publishes to the FrameStore in order.
actor RealSensePump {
    private let store: FrameStore
    private let logger: Logger
    private var config = PumpConfig()
    private var worker: (shared: WorkerShared, consumer: Task<Void, Never>)?

    init(store: FrameStore, logger: Logger) {
        self.store = store
        self.logger = logger
    }

    func start() async {
        guard worker == nil else { return }
        await store.setRunning(true)
        spawnWorker()
    }

    func stop() async {
        guard let worker else {
            await store.setRunning(false)
            return
        }
        self.worker = nil
        worker.shared.stopRequested.store(true, ordering: .sequentiallyConsistent)
        await store.setRunning(false)
        await worker.consumer.value
        await store.clear()
    }

    func configure(width: Int, height: Int, fps: Int, preset: String) async {
        let modeChanged = width != config.width || height != config.height || fps != config.fps
        config = PumpConfig(width: width, height: height, fps: fps, preset: preset)

        guard let worker else { return }
        if modeChanged {
            // Restart the pipeline at the new mode, keeping `running` true so
            // connected MJPEG clients ride through the gap (C++ parity).
            self.worker = nil
            worker.shared.stopRequested.store(true, ordering: .sequentiallyConsistent)
            await worker.consumer.value
            spawnWorker()
        } else {
            worker.shared.pendingPreset.withLock { $0 = preset }
        }
    }

    private func spawnWorker() {
        let config = self.config
        let logger = self.logger

        let shared = WorkerShared()
        shared.pendingPreset.withLock { $0 = config.preset }

        let (frames, continuation) = AsyncStream.makeStream(
            of: [String: [UInt8]].self,
            bufferingPolicy: .bufferingNewest(4)
        )
        let thread = Thread {
            Self.captureLoop(shared: shared, config: config, logger: logger, continuation: continuation)
        }
        thread.name = "realsense-pump"
        thread.start()

        let store = self.store
        let consumer = Task {
            for await updates in frames {
                await store.publish(updates)
            }
            // The capture thread exited. If this wasn't an explicit stop or
            // restart, the pipeline died (no device, USB error) — reflect it.
            if !shared.stopRequested.load(ordering: .sequentiallyConsistent) {
                await store.setRunning(false)
                self.reap(shared)
            }
        }
        worker = (shared, consumer)
    }

    private func reap(_ shared: WorkerShared) {
        if worker?.shared === shared {
            worker = nil
        }
    }

    private static func captureLoop(
        shared: WorkerShared,
        config: PumpConfig,
        logger: Logger,
        continuation: AsyncStream<[String: [UInt8]]>.Continuation
    ) {
        defer { continuation.finish() }

        guard let camera = rsk.cameraCreate() else {
            logger.error("Failed to create RealSense camera")
            return
        }
        defer { rsk.cameraDestroy(camera) }

        guard rsk.cameraStart(camera, Int32(config.width), Int32(config.height), Int32(config.fps)) else {
            logger.error("Failed to start RealSense pipeline at \(config.width)x\(config.height)@\(config.fps)fps")
            return
        }
        defer { rsk.cameraStop(camera) }

        guard let encoder = tjInitCompress() else {
            logger.error("Failed to initialize TurboJPEG encoder")
            return
        }
        defer { tjDestroy(encoder) }

        logger.info("RealSense pipeline started at \(config.width)x\(config.height)@\(config.fps)fps")

        while !shared.stopRequested.load(ordering: .sequentiallyConsistent) {
            let pendingPreset = shared.pendingPreset.withLock { (value: inout String?) -> String? in
                let preset = value
                value = nil
                return preset
            }
            if let preset = pendingPreset {
                let applied = preset.withCString { rsk.cameraApplyPreset(camera, $0) }
                if applied {
                    logger.info("Applied RealSense visual preset: \(preset)")
                }
            }

            let batch = rsk.cameraWaitForFrames(camera, 1000)
            guard batch.ok else { continue }

            var updates: [String: [UInt8]] = [:]
            if let jpeg = JPEGEncoder.encode(batch.color, encoder: encoder) { updates["color"] = jpeg }
            if let jpeg = JPEGEncoder.encode(batch.irLeft, encoder: encoder) { updates["ir-left"] = jpeg }
            if let jpeg = JPEGEncoder.encode(batch.irRight, encoder: encoder) { updates["ir-right"] = jpeg }
            if let jpeg = JPEGEncoder.encode(batch.depth, encoder: encoder) { updates["depth"] = jpeg }
            continuation.yield(updates)
        }
        logger.info("RealSense pipeline stopped")
    }
}
