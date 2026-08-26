import Dispatch
import Foundation
import Synchronization
#if canImport(Glibc)
import Glibc
#elseif canImport(Darwin)
import Darwin
#endif

// ───────────────────────────────────────────────────────────────────────────
// Spawning with an empty signal mask
// ───────────────────────────────────────────────────────────────────────────

// Thread's block parameter is @Sendable and Process is not Sendable on every
// platform, so the handle and any thrown error ride across in a box; the
// semaphore is the happens-before edge that makes reading `error` safe.
private final class SpawnTransfer: @unchecked Sendable {
    let process: Process
    var error: (any Error)?
    init(_ process: Process) { self.process = process }
}

extension Process {
    // posix_spawn() hands the new process the signal mask of the *calling thread*,
    // and a blocked mask survives execve(). Foundation's Process spawns with
    // POSIX_SPAWN_SETPGROUP only — it passes neither POSIX_SPAWN_SETSIGMASK nor
    // POSIX_SPAWN_SETSIGDEF — so whatever the spawning thread has blocked, the child
    // inherits and keeps.
    //
    // On Linux that breaks child teardown, because Swift Concurrency's default
    // executor is libdispatch and every dispatch worker thread calls
    // _dispatch_sigmask(), which blocks every blockable signal. A Process launched
    // from inside a Task therefore starts with SIGTERM and SIGINT blocked — measured
    // SigBlk=fffffffe3bfbea27, against 0000000000000000 for the same spawn on the
    // main thread. terminate() and interrupt() then only ever queue a pending signal
    // that is never delivered, and the child survives everything except SIGKILL and
    // SIGSTOP, the two signals that cannot be blocked.
    //
    // So we spawn from a thread with an empty mask. pthread_create() copies the
    // creator's mask, so the throwaway Thread is not itself the fix — the
    // pthread_sigmask() call inside its body is. A dedicated thread is used instead
    // of clearing and restoring the mask around run() on the current thread because
    // that thread belongs to the concurrency runtime: unblocking there, however
    // briefly, lets a real SIGTERM land on it and bypass graceful shutdown. Here the
    // empty mask is private to a thread that exits right after the spawn.
    //
    // The name is about the mask on purpose: this does not reset signal dispositions,
    // because those are process-wide rather than per-thread. Forcing SIG_DFL here
    // would also disarm NIO's SIGPIPE -> SIG_IGN and the shutdown handlers
    // Hummingbird installs, for the whole server. SIG_IGN is inherited across execve
    // as well, so the child still starts with SIGPIPE ignored; a child that must not
    // inherit that should be launched through `env --default-signal <cmd>`, which
    // resets dispositions and unblocks signals from inside the child, after the exec.

    /// Like `run()`, but the child is spawned from a thread with an empty signal
    /// mask, so it starts with nothing blocked and `terminate()` works.
    ///
    /// Blocks until the spawning thread is done. The work on that thread is a
    /// single posix_spawn, so the wait is sub-millisecond — but it is a blocking
    /// wait, so it does park the calling cooperative-pool thread for that long.
    func runWithEmptySignalMask() throws {
        let transfer = SpawnTransfer(self)
        let spawned = DispatchSemaphore(value: 0)

        let thread = Thread {
            // This thread inherited the mask of whichever dispatch worker created
            // it; drop it before spawning, since the child gets a copy.
            var empty = sigset_t()
            _ = sigemptyset(&empty)
            _ = pthread_sigmask(SIG_SETMASK, &empty, nil)
            do {
                try transfer.process.run()
            } catch {
                transfer.error = error
            }
            spawned.signal()
        }
        thread.name = "process-spawn"
        thread.start()
        spawned.wait()

        if let error = transfer.error { throw error }
    }
}

// ───────────────────────────────────────────────────────────────────────────
// Waiting without blocking a thread
// ───────────────────────────────────────────────────────────────────────────

/// A one-shot latch: `wait()` suspends until the first `signal()`, and returns
/// straight away for every call after that.
///
/// Not `Semaphore`, whose `wait()` returns early when the caller is cancelled —
/// see `waitUntilExit()` below, which must not.
private final class ExitLatch: Sendable {
    private struct State {
        var continuation: CheckedContinuation<Void, Never>?
        var signalled = false
    }

    private let state = Mutex(State())

    /// Suspends until the latch is signalled. At most one caller per latch.
    func wait() async {
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            let resumeNow = state.withLock { s -> Bool in
                if s.signalled { return true }
                s.continuation = continuation
                return false
            }
            // Never resume a continuation while holding the lock.
            if resumeNow { continuation.resume() }
        }
    }

    /// Opens the latch. May be called more than once; only the first one resumes.
    func signal() {
        let continuation = state.withLock { s -> CheckedContinuation<Void, Never>? in
            s.signalled = true
            defer { s.continuation = nil }
            return s.continuation
        }
        continuation?.resume()
    }
}

extension Process {
    /// Like `waitUntilExit()`, but suspends the calling task instead of parking its
    /// thread. In an async context this overload is the one that gets picked.
    ///
    /// Overwrites `terminationHandler`, and leaves it installed once it returns.
    /// Returns immediately for a process that was never launched, like the
    /// synchronous version.
    ///
    /// Deliberately not cancellable: this is what reaps the child, and the caller is
    /// typically *already* cancelled by the time it terminates the child and waits —
    /// returning early there would leave the child running.
    func waitUntilExit() async {
        let latch = ExitLatch()
        // Captures the latch and not self: Process is not Sendable, the handler is.
        terminationHandler = { _ in latch.signal() }
        // Arm first, then check: an exit landing between the two fires the handler,
        // and one that happened before the handler was installed is caught here.
        // Both can fire — the latch absorbs that.
        if !isRunning { latch.signal() }
        await latch.wait()
    }
}
