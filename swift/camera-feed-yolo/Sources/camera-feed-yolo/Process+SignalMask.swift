import Dispatch
import Foundation

// ───────────────────────────────────────────────────────────────────────────
// Process.runWithEmptySignalMask()
//
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
