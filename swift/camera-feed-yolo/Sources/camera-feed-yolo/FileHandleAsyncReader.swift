
import Dispatch
import Foundation
import Synchronization

/// Reads a file handle (typically the read end of a pipe) on a private dispatch
/// source and hands the chunks over to a single async consumer.
///
/// - A zero-length chunk is the end-of-stream sentinel: `read()` returns nil when it
///   reaches it, and for every call after that.
/// - Backpressure: once more than `maxPendingBytes` are queued, the read source is
///   suspended; it resumes once the queue has drained to half the threshold or holds
///   at most one chunk (so a single oversized chunk can never wedge it).
/// - Exactly one concurrent reader is supported; a second one traps.
///
/// `@unchecked Sendable`: all mutable state lives in `state`; `source` and
/// `fileHandle` are immutable and only touched under that lock or from `queue`.
final class FileHandleAsyncReader: @unchecked Sendable {
    private struct State {
        var chunks: [Data] = []      // pushed at the back, popped from the front
        var pendingBytes = 0
        var gated = false            // source is currently suspended for backpressure
        var eofQueued = false        // empty sentinel enqueued; source is done
        var finished = false         // sentinel consumed; read() returns nil from now on
        var readers = 0              // in-flight read() calls; more than one is a bug
    }

    private enum Next {
        case chunk(Data)
        case endOfStream
        case empty
    }

    private let fileHandle: FileHandle
    private let maxPendingBytes: Int
    private let state = Mutex(State())
    private let semaphore = Semaphore()
    private let queue = DispatchQueue(label: "FileHandleAsyncReader")
    private let source: any DispatchSourceRead

    init(fileHandle: FileHandle, maxPendingBytes: Int = 1 << 20) {
        self.fileHandle = fileHandle
        self.maxPendingBytes = maxPendingBytes
        self.source = DispatchSource.makeReadSource(
            fileDescriptor: fileHandle.fileDescriptor,
            queue: queue
        )
        // Weak, otherwise self -> source -> handler -> self keeps the reader alive
        // forever and deinit never runs.
        source.setEventHandler { [weak self] in
            guard let self else { return }
            self.processIncomingData(fileHandle.availableData)
        }
        source.resume()
    }

    deinit {
        // Releasing a suspended dispatch object traps, so un-gate it first. A parked
        // reader holds a strong reference to self, so no continuation can be stranded
        // here.
        let gated = state.withLock { (s) -> Bool in
            let gated = s.gated
            s.gated = false
            return gated
        }
        if gated {
            source.resume()
        }
        source.cancel()
    }

    /// Runs on `queue`, so there is only ever one producer at a time.
    private func processIncomingData(_ data: Data) {
        let wasEmpty = state.withLock { s -> Bool in
            guard !s.eofQueued else { return false }
            if data.isEmpty { s.eofQueued = true }

            let wasEmpty = s.chunks.isEmpty
            s.chunks.append(data)
            s.pendingBytes += data.count
            // Checked after appending, so one chunk may overshoot the threshold;
            // chunks are never split.
            if !s.gated && !s.eofQueued && s.pendingBytes >= maxPendingBytes {
                s.gated = true
                // Suspending under the lock is deliberate, see popLocked().
                source.suspend()
            }
            return wasEmpty
        }
        // Only the empty -> non-empty transition can have a reader waiting on it: a
        // reader parks only after a pop found the queue empty under this same lock, so
        // the first append after that pop is this one. Raising per chunk instead would
        // pile up permits the reader has to spin through, exactly in the backpressure
        // case where it never parks. Never raise under the lock — raise() can resume a
        // continuation.
        if wasEmpty {
            semaphore.raise()
        }
        // A source left running on an EOF-readable fd would spin, enqueuing endless
        // sentinels. From here on the source is never suspended or resumed again.
        if data.isEmpty {
            source.cancel()
        }
    }

    /// Requires the state lock to be held.
    private func popLocked(_ s: inout State) -> Next {
        if s.finished { return .endOfStream }
        guard !s.chunks.isEmpty else { return .empty }

        let data = s.chunks.removeFirst()
        s.pendingBytes -= data.count

        // `chunks.count <= 1` is the escape hatch for a chunk bigger than the whole
        // threshold: without it the queue could sit above half with a single element
        // and never re-open the fd.
        if s.gated && !s.eofQueued
            && (s.pendingBytes <= maxPendingBytes / 2 || s.chunks.count <= 1) {
            s.gated = false
            // Resuming under the lock is deliberate: outside it, the producer could
            // flip gated = true, be preempted, and have this resume() hit a source
            // that is not suspended yet — dispatch traps on over-resume. Neither
            // suspend() nor resume() blocks or re-enters, so holding the lock across
            // them is safe.
            source.resume()
        }

        if data.isEmpty {
            s.finished = true
            return .endOfStream
        }
        return .chunk(data)
    }

    /// Returns the next chunk, or nil on end of stream or cancellation.
    ///
    /// Only one task may call this at a time.
    func read() async -> Data? {
        // A second reader would deadlock at end of stream: the first one latches
        // `finished` and nothing ever raises the semaphore again.
        state.withLock { s in
            precondition(s.readers == 0, "FileHandleAsyncReader supports a single reader")
            s.readers += 1
        }
        defer { state.withLock { $0.readers -= 1 } }

        while true {
            if Task.isCancelled { return nil }
            switch state.withLock({ popLocked(&$0) }) {
            case .chunk(let data): return data
            case .endOfStream: return nil
            // A permit can outlive the chunk it was raised for, when the queue was
            // drained without ever parking — the loop just burns it and waits again.
            // The reverse cannot happen, which is what rules out a lost wakeup.
            case .empty: await semaphore.wait()
            }
        }
    }
}
