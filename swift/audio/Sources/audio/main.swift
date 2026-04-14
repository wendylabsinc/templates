import Foundation
import Hummingbird
import HummingbirdWebSocket

// MARK: - AudioCapture Actor

actor AudioCapture {
    private var process: Process?
    private var clients: [UUID: @Sendable (ByteBuffer) -> Void] = [:]
    private var isRunning = false

    func start() {
        guard !isRunning else { return }
        isRunning = true

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/gst-launch-1.0")
        proc.arguments = [
            "autoaudiosrc", "!",
            "audioconvert", "!",
            "audio/x-raw,format=S16LE,channels=1,rate=16000", "!",
            "fdsink", "fd=1",
        ]

        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = FileHandle.nullDevice

        self.process = proc

        let captureRef = self

        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            var buffer = ByteBuffer()
            buffer.writeBytes(data)
            Task {
                await captureRef.broadcast(buffer)
            }
        }

        do {
            try proc.run()
            print("[AudioCapture] GStreamer pipeline started")
        } catch {
            print("[AudioCapture] Failed to start GStreamer: \(error)")
            isRunning = false
        }
    }

    func stop() {
        process?.terminate()
        process = nil
        isRunning = false
    }

    func addClient(id: UUID, send: @escaping @Sendable (ByteBuffer) -> Void) {
        clients[id] = send
    }

    func removeClient(id: UUID) {
        clients.removeValue(forKey: id)
    }

    private func broadcast(_ buffer: ByteBuffer) {
        for (_, send) in clients {
            send(buffer)
        }
    }
}

// MARK: - Content type helper

func contentType(for path: String) -> String {
    if path.hasSuffix(".html") { return "text/html; charset=utf-8" }
    if path.hasSuffix(".css") { return "text/css; charset=utf-8" }
    if path.hasSuffix(".js") { return "application/javascript; charset=utf-8" }
    if path.hasSuffix(".json") { return "application/json" }
    if path.hasSuffix(".png") { return "image/png" }
    if path.hasSuffix(".jpg") || path.hasSuffix(".jpeg") { return "image/jpeg" }
    if path.hasSuffix(".svg") { return "image/svg+xml" }
    if path.hasSuffix(".wav") { return "audio/wav" }
    if path.hasSuffix(".mp3") { return "audio/mpeg" }
    if path.hasSuffix(".ogg") { return "audio/ogg" }
    if path.hasSuffix(".ico") { return "image/x-icon" }
    return "application/octet-stream"
}

// MARK: - Main

let audioCapture = AudioCapture()
await audioCapture.start()

let router = Router()

// GET / — serve index.html
router.get("/") { _, _ -> Response in
    let path = "./index.html"
    guard let data = FileManager.default.contents(atPath: path) else {
        return Response(status: .notFound, body: .init(byteBuffer: ByteBuffer(string: "Not found")))
    }
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    return Response(
        status: .ok,
        headers: [.contentType: "text/html; charset=utf-8"],
        body: .init(byteBuffer: buffer)
    )
}

// GET /sounds — list .wav files in ./assets
router.get("/sounds") { _, _ -> Response in
    let assetsPath = "./assets"
    let fm = FileManager.default
    var wavFiles: [String] = []
    if let files = try? fm.contentsOfDirectory(atPath: assetsPath) {
        wavFiles = files.filter { $0.hasSuffix(".wav") }.sorted()
    }
    let json = try! JSONEncoder().encode(wavFiles)
    var buffer = ByteBuffer()
    buffer.writeBytes(json)
    return Response(
        status: .ok,
        headers: [.contentType: "application/json"],
        body: .init(byteBuffer: buffer)
    )
}

// GET /assets/* — serve static files
router.get("/assets/{filepath}") { _, context -> Response in
    let filepath = context.parameters.get("filepath") ?? ""
    let fullPath = "./assets/\(filepath)"
    guard let data = FileManager.default.contents(atPath: fullPath) else {
        return Response(status: .notFound, body: .init(byteBuffer: ByteBuffer(string: "Not found")))
    }
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    let ct = contentType(for: filepath)
    return Response(
        status: .ok,
        headers: [.contentType: ct],
        body: .init(byteBuffer: buffer)
    )
}

// WebSocket /stream — send binary PCM data to clients
let wsRouter = Router(context: BasicWebSocketRequestContext.self)
wsRouter.ws("/stream") { inbound, outbound, _ in
    let clientId = UUID()
    print("[WebSocket] Client connected: \(clientId)")

    await audioCapture.addClient(id: clientId) { buffer in
        Task {
            try? await outbound.write(.binary(buffer))
        }
    }

    // Keep connection alive by consuming inbound frames
    for try await _ in inbound {}

    await audioCapture.removeClient(id: clientId)
    print("[WebSocket] Client disconnected: \(clientId)")
}

let app = Application(
    router: router,
    server: .http1WebSocketUpgrade(webSocketRouter: wsRouter),
    configuration: .init(
        address: .hostname("0.0.0.0", port: {{.PORT}})
    )
)

print("[Audio] Server starting on port {{.PORT}}")
try await app.run()
await audioCapture.stop()
