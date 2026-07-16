// Headless continuous speech-to-text: gstreamer-swift mic capture (16 kHz mono
// s16le) → fixed chunks → RMS silence gate → in-process whisper.cpp inference →
// stdout + transcript file. The Swift port of python/whisper-stt.
internal import Foundation
import GStreamer
import CWhisper

#if canImport(Glibc)
import Glibc
#elseif canImport(Darwin)
import Darwin
#endif

// Line-buffer stdout so `wendy device logs` sees each transcription promptly
// (Swift 6 fully-buffers stdout on Linux when not a TTY — see the
// swift-ros2-template-gotchas memory).
setvbuf(stdout, nil, _IOLBF, 0)

// MARK: - Configuration (environment-driven)

let env = ProcessInfo.processInfo.environment

let sampleRate = 16_000
let whisperModel = env["WHISPER_MODEL"] ?? "base.en"
let modelPath = env["WHISPER_MODEL_PATH"]
    ?? "/opt/whisper/models/ggml-\(whisperModel).bin"
let language = env["WHISPER_LANGUAGE"] ?? "en"
let chunkSeconds = Double(env["CHUNK_SECONDS"] ?? "5.0") ?? 5.0
let silenceThreshold = Float(env["SILENCE_THRESHOLD"] ?? "0.01") ?? 0.01
let transcriptFile = env["TRANSCRIPT_FILE"] ?? "/data/transcript.txt"
let samplesPerChunk = Int(chunkSeconds * Double(sampleRate))

// MARK: - Errors

enum WhisperError: Error, CustomStringConvertible {
    case initFailed(String)
    var description: String {
        switch self {
        case .initFailed(let path): return "failed to load whisper model at \(path)"
        }
    }
}

// MARK: - Inference (owns the non-thread-safe whisper context)

actor Transcriber {
    private let ctx: OpaquePointer

    init(modelPath: String) throws {
        var cparams = whisper_context_default_params()
        cparams.use_gpu = true
        guard let ctx = whisper_init_from_file_with_params(modelPath, cparams) else {
            throw WhisperError.initFailed(modelPath)
        }
        self.ctx = ctx
    }

    func transcribe(_ samples: [Float], language: String) -> String {
        var params = whisper_full_default_params(WHISPER_SAMPLING_GREEDY)
        params.print_realtime = false
        params.print_progress = false
        params.print_timestamps = false
        params.no_context = true
        params.n_threads = Int32(max(1, ProcessInfo.processInfo.activeProcessorCount))

        return language.withCString { lang -> String in
            params.language = lang
            let rc = samples.withUnsafeBufferPointer { buf in
                whisper_full(ctx, params, buf.baseAddress, Int32(buf.count))
            }
            guard rc == 0 else { return "" }
            var text = ""
            for i in 0..<whisper_full_n_segments(ctx) {
                if let seg = whisper_full_get_segment_text(ctx, i) {
                    text += String(cString: seg)
                }
            }
            return text.trimmingCharacters(in: .whitespacesAndNewlines)
        }
    }

    deinit { whisper_free(ctx) }
}

// MARK: - Helpers

func rms(_ samples: [Float]) -> Float {
    guard !samples.isEmpty else { return 0 }
    let sumSq = samples.reduce(Float(0)) { $0 + $1 * $1 }
    return (sumSq / Float(samples.count)).squareRoot()
}

func timestamp() -> String {
    ISO8601DateFormatter().string(from: Date())
}

func appendLine(_ line: String, to path: String) {
    let fm = FileManager.default
    let dir = (path as NSString).deletingLastPathComponent
    if !dir.isEmpty {
        try? fm.createDirectory(atPath: dir, withIntermediateDirectories: true)
    }
    if !fm.fileExists(atPath: path) {
        fm.createFile(atPath: path, contents: nil)
    }
    guard let handle = FileHandle(forWritingAtPath: path) else { return }
    defer { try? handle.close() }
    handle.seekToEndOfFile()
    if let data = (line + "\n").data(using: .utf8) {
        handle.write(data)
    }
}

// MARK: - Main

let transcriber = try Transcriber(modelPath: modelPath)

let source = try AudioSource.microphone()
    .withSampleRate(sampleRate)
    .withChannels(1)
    .withFormat(.s16le)
    .build()

print("[whisper-stt] model=\(modelPath) lang=\(language) chunk=\(chunkSeconds)s threshold=\(silenceThreshold)")
print("[whisper-stt] transcript=\(transcriptFile)")
print("[whisper-stt] Listening...")

var pcm: [Float] = []
pcm.reserveCapacity(samplesPerChunk * 2)

for await buffer in source.buffers() {
    buffer.bytes.withUnsafeBytes { raw in
        let count = raw.count / MemoryLayout<Int16>.size
        for i in 0..<count {
            let sample = raw.loadUnaligned(fromByteOffset: i * MemoryLayout<Int16>.size, as: Int16.self)
            pcm.append(Float(sample) / 32768.0)
        }
    }

    while pcm.count >= samplesPerChunk {
        let chunk = Array(pcm[0..<samplesPerChunk])
        pcm.removeFirst(samplesPerChunk)

        if rms(chunk) < silenceThreshold { continue }

        let text = await transcriber.transcribe(chunk, language: language)
        guard !text.isEmpty else { continue }

        let line = "[\(timestamp())] \(text)"
        print(line)
        appendLine(line, to: transcriptFile)
    }
}
