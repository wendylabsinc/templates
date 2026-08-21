
import Synchronization

/// A counting semaphore for async callers: `raise()` adds a permit, `wait()` takes one
/// and suspends the calling task while the count is zero.
///
/// `wait()` also returns when the calling task is cancelled — without taking a permit,
/// so nothing is lost. Callers that care re-check `Task.isCancelled`.
final class Semaphore: Sendable {
    /// One parked caller. Both fields are guarded by the semaphore's mutex; the box
    /// exists so that `wait()` and its cancellation handler can refer to the same
    /// waiter whichever of them runs first.
    private final class Waiter: @unchecked Sendable {
        var continuation: CheckedContinuation<Void, Never>?
        var cancelled = false
    }

    private struct State {
        var value: Int
        var waiters: [Waiter] = []   // FIFO
    }

    private let state: Mutex<State>

    init(value: Int = 0) {
        precondition(value >= 0, "a semaphore cannot start with a negative count")
        self.state = Mutex(State(value: value))
    }

    /// Adds a permit, handing it straight to the oldest waiter if there is one.
    func raise() {
        let continuation = state.withLock { s -> CheckedContinuation<Void, Never>? in
            while !s.waiters.isEmpty {
                let waiter = s.waiters.removeFirst()
                if let continuation = waiter.continuation {
                    waiter.continuation = nil
                    return continuation
                }
            }
            s.value += 1
            return nil
        }
        // Never resume a continuation while holding the lock.
        continuation?.resume()
    }

    /// Takes a permit, suspending until one is available.
    ///
    /// Returns without a permit if the calling task is cancelled.
    func wait() async {
        if Task.isCancelled { return }
        let waiter = Waiter()
        await withTaskCancellationHandler {
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                let resumeNow = state.withLock { s -> Bool in
                    if waiter.cancelled { return true }   // cancelled before parking
                    if s.value > 0 {
                        s.value -= 1
                        return true
                    }
                    waiter.continuation = continuation
                    s.waiters.append(waiter)
                    return false
                }
                if resumeNow { continuation.resume() }
            }
        } onCancel: {
            let continuation = state.withLock { s -> CheckedContinuation<Void, Never>? in
                waiter.cancelled = true
                // Nil when the waiter has not parked yet — the body above sees the flag
                // and bails out — or when it already resumed, in which case this box is
                // about to be dropped.
                guard let continuation = waiter.continuation else { return nil }
                waiter.continuation = nil
                s.waiters.removeAll { $0 === waiter }
                return continuation
            }
            continuation?.resume()
        }
    }
}
