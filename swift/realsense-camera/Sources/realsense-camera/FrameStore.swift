internal import Foundation

struct FrameSnapshot: Sendable {
    let jpeg: [UInt8]
    let sequence: UInt64
}

struct HealthStatus: Encodable {
    let streams: [String]
    let running: Bool
    let fps: [String: Double]
}

// Holds the latest encoded frame per stream and wakes waiting MJPEG handlers
// on publish. Mirrors the C++ template's RealSensePump frame/fps bookkeeping.
actor FrameStore {
    static let streamIds = ["color", "ir-left", "ir-right", "depth"]

    private var latest: [String: FrameSnapshot] = [:]
    private(set) var running = false
    private var waiters: [UUID: CheckedContinuation<Void, Never>] = [:]

    private var fpsCounts: [String: Int] = [:]
    private var fpsLatest: [String: Double] = [:]
    private var fpsWindowStart = ContinuousClock.now

    func setRunning(_ value: Bool) {
        running = value
        if !value {
            resetFPS()
        }
        notifyAll()
    }

    func clear() {
        latest.removeAll()
        resetFPS()
        notifyAll()
    }

    func publish(_ updates: [String: [UInt8]]) {
        guard !updates.isEmpty else { return }
        for (stream, jpeg) in updates {
            let sequence = (latest[stream]?.sequence ?? 0) + 1
            latest[stream] = FrameSnapshot(jpeg: jpeg, sequence: sequence)
            fpsCounts[stream, default: 0] += 1
        }

        let elapsed = fpsWindowStart.duration(to: .now)
        if elapsed >= .seconds(1) {
            let seconds = Double(elapsed.components.seconds)
                + Double(elapsed.components.attoseconds) / 1e18
            for stream in Self.streamIds {
                fpsLatest[stream] = (Double(fpsCounts[stream] ?? 0) / seconds * 10).rounded() / 10
                fpsCounts[stream] = 0
            }
            fpsWindowStart = .now
        }
        notifyAll()
    }

    func health() -> HealthStatus {
        HealthStatus(
            streams: Self.streamIds,
            running: running,
            fps: Dictionary(uniqueKeysWithValues: Self.streamIds.map { ($0, fpsLatest[$0] ?? 0) })
        )
    }

    // Returns the next frame for `stream` newer than `sequence`. Returns nil
    // when the pump is stopped or when `timeout` passes with no new frame —
    // the MJPEG handler then ends its response (same semantics as the C++
    // template's waitForFrame).
    func waitForFrame(stream: String, after sequence: UInt64, timeout: Duration) async -> FrameSnapshot? {
        let deadline = ContinuousClock.now + timeout
        while true {
            if let snapshot = latest[stream], snapshot.sequence != sequence {
                return snapshot
            }
            if !running {
                return nil
            }
            let remaining = ContinuousClock.now.duration(to: deadline)
            if remaining <= .zero {
                return nil
            }
            await waitForChange(upTo: remaining)
        }
    }

    private func waitForChange(upTo duration: Duration) async {
        let id = UUID()
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            waiters[id] = continuation
            Task {
                try? await Task.sleep(for: duration)
                self.expire(id)
            }
        }
    }

    private func expire(_ id: UUID) {
        waiters.removeValue(forKey: id)?.resume()
    }

    private func notifyAll() {
        let pending = waiters
        waiters.removeAll()
        for (_, continuation) in pending {
            continuation.resume()
        }
    }

    private func resetFPS() {
        fpsCounts.removeAll()
        for stream in Self.streamIds {
            fpsLatest[stream] = 0
        }
        fpsWindowStart = .now
    }
}
