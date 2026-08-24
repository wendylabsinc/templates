internal import Foundation
import GStreamer
import Hummingbird
import HummingbirdWebSocket
import Logging
import OTel
import ServiceLifecycle

struct AudioDevice: ResponseCodable {
    let id: String
    let name: String
}

struct SoundFile: ResponseCodable {
    let name: String
    let file: String
}

struct StatusResponse: ResponseCodable {
    let status: String
    let speaker: String?
    let file: String?
}

actor SpeakerSelection {
    private var deviceID: String?
    func set(_ deviceID: String) { self.deviceID = deviceID }
    func get() -> String? { deviceID }
}

// MARK: - AudioCapture Actor

actor AudioCapture {
    private let logger: Logger
    private var source: AudioSource?
    private var captureTask: Task<Void, Never>?
    private var clients: [UUID: @Sendable (ByteBuffer) async -> Void] = [:]
    private var currentDevice: String?

    init(logger: Logger) {
        self.logger = logger
    }

    func start() {
        guard source == nil else { return }

        do {
            let builder: AudioSourceBuilder
            if let currentDevice {
                builder = try AudioSource.microphone(devicePath: currentDevice)
            } else {
                builder = AudioSource.microphone()
            }

            let source = try builder
                .withSampleRate(16_000)
                .withChannels(1)
                .withFormat(.s16le)
                .build()
            self.source = source

            captureTask = Task {
                for await buffer in source.buffers() {
                    var chunk = ByteBuffer()
                    _ = buffer.bytes.withUnsafeBytes { raw in
                        chunk.writeBytes(raw)
                    }
                    guard chunk.readableBytes > 0 else { continue }
                    await self.broadcast(chunk)
                }
            }

            logger.info("GStreamer audio pipeline started")
        } catch {
            logger.error(
                "Failed to start GStreamer audio pipeline",
                metadata: ["error": "\(error)"]
            )
            source = nil
        }
    }

    func stop() async {
        captureTask?.cancel()
        captureTask = nil
        if let source {
            await source.stop()
        }
        source = nil
    }

    /// Returns true if the capture pipeline restarted successfully. `start()`
    /// leaves `source` nil when the GStreamer pipeline fails to build.
    func switchMicrophone(to deviceID: String) async -> Bool {
        currentDevice = deviceID
        await stop()
        start()
        return source != nil
    }

    func addClient(id: UUID, send: @escaping @Sendable (ByteBuffer) async -> Void) {
        clients[id] = send
    }

    func removeClient(id: UUID) {
        clients.removeValue(forKey: id)
    }

    private func broadcast(_ buffer: ByteBuffer) async {
        await withTaskGroup(of: Void.self) { group in
            for (_, send) in clients {
                group.addTask { await send(buffer) }
            }
        }
    }
}

// MARK: - Playback

actor PlaybackManager {
    private var task: Task<Void, Never>?

    func play(pipeline: Pipeline) {
        task?.cancel()
        task = Task {
            for await message in pipeline.bus.messages(filter: [.eos, .error]) {
                if case .eos = message { break }
                if case .error = message { break }
            }
            pipeline.stop()
        }
    }

    func stop() {
        task?.cancel()
        task = nil
    }
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
    proc.executableURL = URL(filePath: "/usr/bin/\(executable)")
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

func playSound(filename: String, speaker: String?, playback: PlaybackManager) async -> Bool {
    guard let path = resolveSoundPath(filename) else { return false }
    let sink = speaker.map { "alsasink device=\($0)" } ?? "autoaudiosink"
    let description =
        "filesrc location=\"\(path)\" ! wavparse ! audioconvert ! audioresample ! \(sink)"
    do {
        let pipeline = try Pipeline(description)
        try pipeline.play()
        await playback.play(pipeline: pipeline)
        return true
    } catch {
        return false
    }
}

private struct SwitchMicrophoneMessage: Decodable {
    let switch_microphone: String
}

private struct MicSwitchedAck: Encodable {
    let type: String
    let device: String?

    init(ok: Bool, device: String) {
        type = ok ? "mic_switched" : "mic_switch_failed"
        self.device = ok ? device : nil
    }
}

/// Handle an inbound text command. Returns the switch acknowledgement to send
/// back (success reflects whether the capture pipeline restarted), or nil if
/// it was not a switch command.
private func handleWebSocketText(_ text: String, audioCapture: AudioCapture) async -> MicSwitchedAck? {
    guard let data = text.data(using: .utf8),
          let msg = try? JSONDecoder().decode(SwitchMicrophoneMessage.self, from: data)
    else { return nil }
    let ok = await audioCapture.switchMicrophone(to: msg.switch_microphone)
    return MicSwitchedAck(ok: ok, device: msg.switch_microphone)
}

// MARK: - Main

struct AudioService: Service {
    let logger: Logger

    func run() async throws {
        let audioCapture = AudioCapture(logger: logger)
        let speakerSelection = SpeakerSelection()
        let playbackManager = PlaybackManager()
        await audioCapture.start()

        let router = Router()

        router.get("/sounds") { _, _ in listSounds() }
        router.get("/microphones") { _, _ in parseAudioDevices("arecord") }
        router.get("/speakers") { _, _ in parseAudioDevices("aplay") }

        router.post("/speaker/{deviceID}") { _, context -> StatusResponse in
            guard let deviceID = context.parameters.get("deviceID") else {
                throw HTTPError(.badRequest, message: "missing speaker")
            }
            await speakerSelection.set(deviceID)
            return StatusResponse(status: "ok", speaker: deviceID, file: nil)
        }

        router.post("/play/{filename}") { _, context -> StatusResponse in
            guard let filename = context.parameters.get("filename") else {
                throw HTTPError(.notFound, message: "not found")
            }
            guard await playSound(filename: filename, speaker: speakerSelection.get(), playback: playbackManager) else {
                throw HTTPError(.notFound, message: "not found")
            }
            return StatusResponse(status: "playing", speaker: nil, file: filename)
        }

        router.get("/", use: spaHandler(staticDir: "."))
        router.get("{path+}", use: spaHandler(staticDir: "."))

        let wsRouter = Router(context: BasicWebSocketRequestContext.self)
        wsRouter.ws("/stream") { inbound, outbound, _ in
            let clientId = UUID()
            logger.info(
                "WebSocket client connected",
                metadata: ["client.id": "\(clientId)"]
            )

            await audioCapture.addClient(id: clientId) { buffer in
                try? await outbound.write(.binary(buffer))
            }

            for try await input in inbound.messages(maxSize: 1_000_000) {
                guard case .text(let text) = input else { continue }
                if let ack = await handleWebSocketText(text, audioCapture: audioCapture),
                   let data = try? JSONEncoder().encode(ack),
                   let json = String(data: data, encoding: .utf8) {
                    // Acknowledge so the UI can leave the "Switching" state.
                    try? await outbound.write(.text(json))
                }
            }

            await audioCapture.removeClient(id: clientId)
            logger.info(
                "WebSocket client disconnected",
                metadata: ["client.id": "\(clientId)"]
            )
        }

        let app = Application(
            router: router,
            server: .http1WebSocketUpgrade(webSocketRouter: wsRouter),
            configuration: .init(address: .hostname("0.0.0.0", port: {{.PORT}}))
        )

        logger.info(
            "Audio server starting",
            metadata: ["server.address": "0.0.0.0", "server.port": "{{.PORT}}"]
        )
        do {
            try await app.runService()
        } catch {
            await audioCapture.stop()
            throw error
        }
        await audioCapture.stop()
    }
}

let observability = try OTel.bootstrap()
let logger = Logger(label: "{{.APP_ID}}")
try await ServiceGroup(
    services: [observability, AudioService(logger: logger)],
    gracefulShutdownSignals: [.sigterm, .sigint],
    logger: logger
).run()
