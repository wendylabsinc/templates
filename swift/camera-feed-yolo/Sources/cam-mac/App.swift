import Foundation
import WendyLiteAVSource

// ───────────────────────────────────────────────────────────────────────────
// cam-mac — a macOS-side smoke test for WendyLiteAVSource.
//
// Connects to a wendy-lite AV source, then writes every frame it receives to
// /tmp/cam-mac as a JPEG until interrupted. No inference, no server: this
// exists so the wire protocol and the chunk reassembler can be checked by eye
// from a dev machine, where the `cam` target's Linux-only dependencies
// (GStreamer, ONNX Runtime) aren't available.
// ───────────────────────────────────────────────────────────────────────────

// An actor so the @Sendable frame handler stays a plain closure: the file
// counter and the watchdog timestamp are the only mutable state, and they live
// here rather than crossing isolation boundaries.
actor FrameWriter {
    private let directory: URL
    private let clock = ContinuousClock()
    private var saved: UInt32 = 0
    private var lastFrameAt: ContinuousClock.Instant

    init(directory: URL) {
        self.directory = directory
        // Starts the idle window at launch so a source that never sends a
        // single frame times out like one that stops halfway.
        self.lastFrameAt = ContinuousClock.now
    }

    // Numbered from our own counter, not from `frame.number`: the device's
    // counter restarts at 0 when it reboots, which would overwrite the files
    // already on disk.
    func save(_ frame: VideoFrame) {
        saved += 1
        lastFrameAt = clock.now

        let name = String(format: "frame-%06u.jpg", saved)
        let url = directory.appendingPathComponent(name)
        do {
            try frame.jpeg.write(to: url)
            print("[cam-mac] \(url.path) — \(frame.jpeg.count) bytes (device frame \(frame.number))")
        } catch {
            // Printed, never thrown: one failed write must not tear down the
            // stream that is still delivering frames.
            print("[cam-mac] write failed for \(name): \(error)")
        }
    }

    func idleSeconds() -> Int {
        Int((clock.now - lastFrameAt).components.seconds)
    }
}

@main
struct CamMacApp {
    private static let outputDirectory = "/tmp/cam-mac"
    private static let idleTimeoutSeconds = 10

    static func main() async throws {
        let args = Array(CommandLine.arguments.dropFirst())
        let env = ProcessInfo.processInfo.environment

        guard let host = args.first ?? env["WENDY_AV_HOST"], !host.isEmpty else {
            usage()
        }

        let portArgument: String? = args.count > 1 ? args[1] : env["WENDY_AV_PORT"]
        let port: Int
        if let portArgument {
            guard let parsed = Int(portArgument), (1...65_535).contains(parsed) else {
                usage()
            }
            port = parsed
        } else {
            port = AVSource.defaultPort
        }

        // A subdirectory rather than bare /tmp: the run is unbounded, so the
        // frames need to stay easy to list and to delete in one go.
        try FileManager.default.createDirectory(
            atPath: outputDirectory, withIntermediateDirectories: true)

        let writer = FrameWriter(directory: URL(fileURLWithPath: outputDirectory))
        let source = AVSource(host: host, port: port)

        // Registered before start(): start() sends the first request as soon
        // as it connects, so a handler added afterwards could miss frame one.
        _ = await source.subscribeToVideoFrames { frame in
            await writer.save(frame)
        }
        try await source.start()

        print("[cam-mac] saving frames to \(outputDirectory) — ^C to stop")

        // start() returns once connected and leaves the request/receive loop
        // running in the background, so main has to stay alive on its own.
        // Polling rather than sleeping forever: when the device goes away the
        // loop just stops, and a silent hang is indistinguishable from a
        // working stream.
        while true {
            try await Task.sleep(for: .seconds(5))
            let idle = await writer.idleSeconds()
            if idle >= idleTimeoutSeconds {
                print("[cam-mac] no frames for \(idle)s — exiting")
                await source.stop()
                exit(1)
            }
        }
    }

    private static func usage() -> Never {
        print("usage: cam-mac <host> [port]")
        print("  host defaults to $WENDY_AV_HOST, port to $WENDY_AV_PORT then \(AVSource.defaultPort)")
        print("  writes every frame to \(outputDirectory)/frame-NNNNNN.jpg until interrupted")
        exit(2)
    }
}
