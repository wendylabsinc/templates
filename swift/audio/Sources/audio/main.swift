import Foundation
import Hummingbird
import HummingbirdWebSocket

struct AudioDevice: Codable {
    let id: String
    let name: String
}

struct SoundFile: Codable {
    let name: String
    let file: String
}

struct StatusResponse: Codable {
    let status: String
    let speaker: String?
    let file: String?
}

struct ErrorResponse: Codable {
    let error: String
}

final class SpeakerSelection: @unchecked Sendable {
    private let lock = NSLock()
    private var deviceID: String?

    func set(_ deviceID: String) {
        lock.lock()
        self.deviceID = deviceID
        lock.unlock()
    }

    func get() -> String? {
        lock.lock()
        defer { lock.unlock() }
        return deviceID
    }
}

// MARK: - AudioCapture Actor

actor AudioCapture {
    private var process: Process?
    private var outputPipe: Pipe?
    private var clients: [UUID: @Sendable (ByteBuffer) -> Void] = [:]
    private var currentDevice: String?
    private var isRunning = false
    private var leftover = Data()

    func start() {
        guard !isRunning else { return }
        isRunning = true

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/gst-launch-1.0")

        let sourceArgs: [String]
        if let currentDevice {
            sourceArgs = ["alsasrc", "device=\(currentDevice)"]
        } else {
            sourceArgs = ["autoaudiosrc"]
        }

        proc.arguments = sourceArgs + [
            "!",
            "audioconvert",
            "!",
            "audioresample",
            "!",
            "audio/x-raw,format=S16LE,channels=1,rate=16000",
            "!",
            "fdsink",
            "fd=1",
        ]

        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = FileHandle.nullDevice

        self.process = proc
        self.outputPipe = pipe

        let captureRef = self

        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            Task {
                await captureRef.ingest(data)
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
        outputPipe?.fileHandleForReading.readabilityHandler = nil
        process?.terminate()
        outputPipe = nil
        process = nil
        isRunning = false
        leftover.removeAll(keepingCapacity: false)
    }

    private func ingest(_ chunk: Data) {
        // S16LE samples are 2 bytes — only forward aligned slices and carry
        // any trailing odd byte into the next ingest. Otherwise the browser
        // throws "byte length of Int16Array should be a multiple of 2".
        leftover.append(chunk)
        let aligned = leftover.count - (leftover.count % 2)
        guard aligned > 0 else { return }
        let payload = leftover.prefix(aligned)
        leftover.removeFirst(aligned)
        var buffer = ByteBuffer()
        buffer.writeBytes(payload)
        broadcast(buffer)
    }

    func switchMicrophone(to deviceID: String) {
        currentDevice = deviceID
        stop()
        start()
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

// MARK: - Helpers

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

func jsonResponse<T: Encodable>(_ value: T) -> Response {
    let data = try! JSONEncoder().encode(value)
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    return Response(
        status: .ok,
        headers: [.contentType: "application/json"],
        body: .init(byteBuffer: buffer)
    )
}

func jsonError(_ message: String, status: HTTPResponse.Status) -> Response {
    let data = try! JSONEncoder().encode(ErrorResponse(error: message))
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    return Response(
        status: status,
        headers: [.contentType: "application/json"],
        body: .init(byteBuffer: buffer)
    )
}

func displayName(for file: String) -> String {
    let stem = file.hasSuffix(".wav") ? String(file.dropLast(4)) : file
    var result = ""
    var capitalizeNext = true

    for char in stem {
        if char == "-" || char == "_" {
            result.append(" ")
            capitalizeNext = true
        } else if capitalizeNext {
            result.append(String(char).uppercased())
            capitalizeNext = false
        } else {
            result.append(char)
        }
    }

    return result
}

func listSounds() -> [SoundFile] {
    let assetsPath = "./assets"
    let fm = FileManager.default
    guard let files = try? fm.contentsOfDirectory(atPath: assetsPath) else {
        return []
    }

    return files
        .filter { $0.hasSuffix(".wav") }
        .sorted()
        .map { SoundFile(name: displayName(for: $0), file: $0) }
}

func parseAudioDevices(_ executable: String) -> [AudioDevice] {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/bin/\(executable)")
    proc.arguments = ["-l"]
    proc.standardError = FileHandle.nullDevice

    let pipe = Pipe()
    proc.standardOutput = pipe

    do {
        try proc.run()
    } catch {
        return []
    }

    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    proc.waitUntilExit()
    guard let output = String(data: data, encoding: .utf8) else {
        return []
    }

    // `arecord -l` / `aplay -l` lines look like:
    //   card 0: PCH [HDA Intel PCH], device 3: HDMI 0 [HDMI 0]
    // HDMI outputs commonly use device 3, 7, etc., so we must capture the
    // device number alongside the card number and dedupe on the pair.
    var seen = Set<String>()
    var devices: [AudioDevice] = []
    for line in output.split(separator: "\n") {
        guard line.hasPrefix("card ") else { continue }
        guard let deviceRange = line.range(of: ", device ") else { continue }
        let cardPortion = line[..<deviceRange.lowerBound]
        let devicePortion = line[deviceRange.upperBound...]

        let cardSplit = cardPortion.split(separator: ":", maxSplits: 1)
        guard cardSplit.count == 2 else { continue }
        let cardWords = cardSplit[0].split(separator: " ")
        guard cardWords.count >= 2 else { continue }
        let cardNum = String(cardWords[1])
        let cardName = cardSplit[1]
            .split(separator: "[", maxSplits: 1)
            .first
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) } ?? ""

        let deviceSplit = devicePortion.split(separator: ":", maxSplits: 1)
        guard deviceSplit.count == 2 else { continue }
        let deviceNum = deviceSplit[0].trimmingCharacters(in: .whitespacesAndNewlines)
        let deviceName = deviceSplit[1]
            .split(separator: "[", maxSplits: 1)
            .first
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) } ?? ""

        let id = "hw:\(cardNum),\(deviceNum)"
        guard seen.insert(id).inserted else { continue }

        let display: String
        if !cardName.isEmpty && !deviceName.isEmpty {
            display = "\(cardName) - \(deviceName)"
        } else if !deviceName.isEmpty {
            display = deviceName
        } else if !cardName.isEmpty {
            display = cardName
        } else {
            display = "Card \(cardNum) device \(deviceNum)"
        }

        devices.append(AudioDevice(id: id, name: display))
    }
    return devices
}

func resolveSoundPath(_ filename: String) -> String? {
    guard !filename.contains("/"),
          !filename.contains("\\"),
          filename.lowercased().hasSuffix(".wav") else {
        return nil
    }

    let path = "./assets/\(filename)"
    return FileManager.default.fileExists(atPath: path) ? path : nil
}

func playSound(filename: String, speaker: String?) -> Bool {
    guard let path = resolveSoundPath(filename) else {
        return false
    }

    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/bin/gst-launch-1.0")
    var args = [
        "filesrc",
        "location=\(path)",
        "!",
        "wavparse",
        "!",
        "audioconvert",
        "!",
        "audioresample",
        "!",
    ]

    if let speaker {
        args += ["alsasink", "device=\(speaker)"]
    } else {
        args += ["autoaudiosink"]
    }

    proc.arguments = args
    proc.standardOutput = FileHandle.nullDevice
    proc.standardError = FileHandle.nullDevice

    do {
        try proc.run()
        return true
    } catch {
        return false
    }
}

func handleWebSocketText(_ text: String, audioCapture: AudioCapture) async {
    guard let data = text.data(using: .utf8),
          let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let deviceID = json["switch_microphone"] as? String else {
        return
    }

    await audioCapture.switchMicrophone(to: deviceID)
}

// MARK: - Main

let audioCapture = AudioCapture()
let speakerSelection = SpeakerSelection()
await audioCapture.start()

let router = Router()

// GET / - serve index.html
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

// GET /sounds - list .wav files in ./assets
router.get("/sounds") { _, _ -> Response in
    jsonResponse(listSounds())
}

// GET /microphones - list ALSA capture devices
router.get("/microphones") { _, _ -> Response in
    jsonResponse(parseAudioDevices("arecord"))
}

// GET /speakers - list ALSA playback devices
router.get("/speakers") { _, _ -> Response in
    jsonResponse(parseAudioDevices("aplay"))
}

// POST /speaker/{deviceID} - set active playback device
router.post("/speaker/{deviceID}") { _, context -> Response in
    guard let deviceID = context.parameters.get("deviceID") else {
        return jsonError("missing speaker", status: .badRequest)
    }
    speakerSelection.set(deviceID)
    return jsonResponse(StatusResponse(status: "ok", speaker: deviceID, file: nil))
}

// POST /play/{filename} - play a bundled .wav file
router.post("/play/{filename}") { _, context -> Response in
    guard let filename = context.parameters.get("filename") else {
        return jsonError("not found", status: .notFound)
    }

    guard playSound(filename: filename, speaker: speakerSelection.get()) else {
        return jsonError("not found", status: .notFound)
    }

    return jsonResponse(StatusResponse(status: "playing", speaker: nil, file: filename))
}

// GET /assets/* - serve static files
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

// WebSocket /stream - send binary PCM data to clients
let wsRouter = Router(context: BasicWebSocketRequestContext.self)
wsRouter.ws("/stream") { inbound, outbound, _ in
    let clientId = UUID()
    print("[WebSocket] Client connected: \(clientId)")

    await audioCapture.addClient(id: clientId) { buffer in
        Task {
            try? await outbound.write(.binary(buffer))
        }
    }

    for try await input in inbound.messages(maxSize: 1_000_000) {
        guard case .text(let text) = input else {
            continue
        }
        await handleWebSocketText(text, audioCapture: audioCapture)
    }

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
