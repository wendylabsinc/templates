import Foundation
import Hummingbird
import HummingbirdWebSocket
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

// ---------------------------------------------------------------------------
// MARK: - SQLite Helpers (C interop via libsqlite3)
// ---------------------------------------------------------------------------

// We use the system SQLite3 C library directly to avoid external dependencies.
// On Linux this requires libsqlite3-dev at build time and libsqlite3-0 at run time.

#if canImport(SQLite3)
import SQLite3
#elseif canImport(CSQLite)
import CSQLite
#else
// Fallback: declare the symbols we need so the code compiles everywhere
// as long as -lsqlite3 is passed to the linker.
@_silgen_name("sqlite3_open")
func sqlite3_open(_ filename: UnsafePointer<CChar>?, _ ppDb: UnsafeMutablePointer<OpaquePointer?>?) -> Int32
@_silgen_name("sqlite3_close")
func sqlite3_close(_ db: OpaquePointer?) -> Int32
@_silgen_name("sqlite3_exec")
func sqlite3_exec(_ db: OpaquePointer?, _ sql: UnsafePointer<CChar>?,
                  _ callback: (@convention(c) (UnsafeMutableRawPointer?, Int32,
                  UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>?,
                  UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>?) -> Int32)?,
                  _ context: UnsafeMutableRawPointer?,
                  _ errmsg: UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>?) -> Int32
@_silgen_name("sqlite3_prepare_v2")
func sqlite3_prepare_v2(_ db: OpaquePointer?, _ sql: UnsafePointer<CChar>?,
                        _ nByte: Int32, _ ppStmt: UnsafeMutablePointer<OpaquePointer?>?,
                        _ pzTail: UnsafeMutablePointer<UnsafePointer<CChar>?>?) -> Int32
@_silgen_name("sqlite3_step")
func sqlite3_step(_ stmt: OpaquePointer?) -> Int32
@_silgen_name("sqlite3_finalize")
func sqlite3_finalize(_ stmt: OpaquePointer?) -> Int32
@_silgen_name("sqlite3_column_count")
func sqlite3_column_count(_ stmt: OpaquePointer?) -> Int32
@_silgen_name("sqlite3_column_name")
func sqlite3_column_name(_ stmt: OpaquePointer?, _ N: Int32) -> UnsafePointer<CChar>?
@_silgen_name("sqlite3_column_type")
func sqlite3_column_type(_ stmt: OpaquePointer?, _ N: Int32) -> Int32
@_silgen_name("sqlite3_column_int64")
func sqlite3_column_int64(_ stmt: OpaquePointer?, _ N: Int32) -> Int64
@_silgen_name("sqlite3_column_text")
func sqlite3_column_text(_ stmt: OpaquePointer?, _ N: Int32) -> UnsafePointer<UInt8>?
@_silgen_name("sqlite3_bind_text")
func sqlite3_bind_text(_ stmt: OpaquePointer?, _ idx: Int32,
                       _ value: UnsafePointer<CChar>?, _ n: Int32,
                       _ destructor: (@convention(c) (UnsafeMutableRawPointer?) -> Void)?) -> Int32
@_silgen_name("sqlite3_bind_int64")
func sqlite3_bind_int64(_ stmt: OpaquePointer?, _ idx: Int32, _ value: Int64) -> Int32
@_silgen_name("sqlite3_last_insert_rowid")
func sqlite3_last_insert_rowid(_ db: OpaquePointer?) -> Int64
@_silgen_name("sqlite3_changes")
func sqlite3_changes(_ db: OpaquePointer?) -> Int32
@_silgen_name("sqlite3_errmsg")
func sqlite3_errmsg(_ db: OpaquePointer?) -> UnsafePointer<CChar>?

let SQLITE_OK: Int32       = 0
let SQLITE_ROW: Int32      = 100
let SQLITE_DONE: Int32     = 101
let SQLITE_INTEGER: Int32  = 1
let SQLITE_TEXT: Int32     = 3
let SQLITE_NULL: Int32     = 5
let SQLITE_TRANSIENT = unsafeBitCast(-1, to: (@convention(c) (UnsafeMutableRawPointer?) -> Void).self)
#endif

/// Lightweight wrapper around a SQLite3 database pointer.
final class SQLiteDB: @unchecked Sendable {
    private let db: OpaquePointer?

    init(path: String) throws {
        // Ensure parent directory exists
        let dir = (path as NSString).deletingLastPathComponent
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)

        var handle: OpaquePointer?
        guard sqlite3_open(path, &handle) == SQLITE_OK else {
            let msg = handle.flatMap { sqlite3_errmsg($0) }.map { String(cString: $0) } ?? "unknown"
            throw SQLiteError.open(msg)
        }
        self.db = handle
    }

    deinit { _ = sqlite3_close(db) }

    @discardableResult
    func exec(_ sql: String) throws -> [[String: String?]] {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
            let msg = sqlite3_errmsg(db).map { String(cString: $0) } ?? "unknown"
            throw SQLiteError.prepare(msg)
        }
        defer { _ = sqlite3_finalize(stmt) }

        var rows: [[String: String?]] = []
        let colCount = sqlite3_column_count(stmt)
        while sqlite3_step(stmt) == SQLITE_ROW {
            var row: [String: String?] = [:]
            for i in 0..<colCount {
                let name = sqlite3_column_name(stmt, i).map { String(cString: $0) } ?? "column_\(i)"
                let type = sqlite3_column_type(stmt, i)
                if type == SQLITE_NULL {
                    row[name] = nil
                } else if type == SQLITE_INTEGER {
                    row[name] = String(sqlite3_column_int64(stmt, i))
                } else {
                    row[name] = sqlite3_column_text(stmt, i).map { String(cString: $0) }
                }
            }
            rows.append(row)
        }
        return rows
    }

    @discardableResult
    func run(_ sql: String, bindings: [Any?] = []) throws -> Int64 {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
            let msg = sqlite3_errmsg(db).map { String(cString: $0) } ?? "unknown"
            throw SQLiteError.prepare(msg)
        }
        defer { _ = sqlite3_finalize(stmt) }

        for (i, value) in bindings.enumerated() {
            let idx = Int32(i + 1)
            switch value {
            case let v as String:
                _ = sqlite3_bind_text(stmt, idx, v, -1, SQLITE_TRANSIENT)
            case let v as Int:
                _ = sqlite3_bind_int64(stmt, idx, Int64(v))
            case let v as Int64:
                _ = sqlite3_bind_int64(stmt, idx, v)
            default:
                // NULL binding is the default
                break
            }
        }

        let rc = sqlite3_step(stmt)
        guard rc == SQLITE_DONE || rc == SQLITE_ROW else {
            let msg = sqlite3_errmsg(db).map { String(cString: $0) } ?? "unknown"
            throw SQLiteError.step(msg)
        }
        return sqlite3_last_insert_rowid(db)
    }

    var changes: Int32 { sqlite3_changes(db) }
}

enum SQLiteError: Error, CustomStringConvertible {
    case open(String), prepare(String), step(String)
    var description: String {
        switch self {
        case .open(let m): "sqlite3_open: \(m)"
        case .prepare(let m): "sqlite3_prepare: \(m)"
        case .step(let m): "sqlite3_step: \(m)"
        }
    }
}

// ---------------------------------------------------------------------------
// MARK: - Car Models
// ---------------------------------------------------------------------------

struct Car: Codable, Sendable {
    let id: Int
    let make: String
    let model: String
    let color: String
    let year: Int
    let created_at: String?
    let updated_at: String?
}

struct CarInput: Decodable, Sendable {
    let make: String
    let model: String
    let color: String
    let year: Int
}

// ---------------------------------------------------------------------------
// MARK: - CarStore (actor-isolated SQLite access)
// ---------------------------------------------------------------------------

actor CarStore {
    private let db: SQLiteDB

    init(path: String = "/data/cars.db") throws {
        self.db = try SQLiteDB(path: path)
        try db.exec("""
            CREATE TABLE IF NOT EXISTS cars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                make TEXT NOT NULL,
                model TEXT NOT NULL,
                color TEXT NOT NULL,
                year INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT
            )
        """)
    }

    func all() throws -> [Car] {
        let rows = try db.exec("SELECT * FROM cars ORDER BY id")
        return rows.compactMap(carFromRow)
    }

    func get(id: Int) throws -> Car? {
        let rows = try db.exec("SELECT * FROM cars WHERE id = \(id)")
        return rows.first.flatMap(carFromRow)
    }

    func create(input: CarInput) throws -> Car? {
        let rowId = try db.run(
            "INSERT INTO cars (make, model, color, year) VALUES (?, ?, ?, ?)",
            bindings: [input.make, input.model, input.color, input.year]
        )
        let rows = try db.exec("SELECT * FROM cars WHERE id = \(rowId)")
        return rows.first.flatMap(carFromRow)
    }

    func update(id: Int, input: CarInput) throws -> Car? {
        try db.run(
            "UPDATE cars SET make=?, model=?, color=?, year=?, updated_at=datetime('now') WHERE id=?",
            bindings: [input.make, input.model, input.color, input.year, id]
        )
        return try get(id: id)
    }

    func delete(id: Int) throws -> Bool {
        try db.run("DELETE FROM cars WHERE id = ?", bindings: [id])
        return db.changes > 0
    }

    private func carFromRow(_ row: [String: String?]) -> Car? {
        guard let idStr = row["id"] ?? nil, let id = Int(idStr),
              let make = row["make"] ?? nil,
              let model = row["model"] ?? nil,
              let color = row["color"] ?? nil,
              let yearStr = row["year"] ?? nil, let year = Int(yearStr) else { return nil }
        return Car(
            id: id, make: make, model: model, color: color, year: year,
            created_at: row["created_at"] ?? nil,
            updated_at: row["updated_at"] ?? nil
        )
    }
}

// ---------------------------------------------------------------------------
// MARK: - Shell Helpers
// ---------------------------------------------------------------------------

/// Run a command and return stdout as a string. Returns nil on failure.
func shell(_ args: [String], timeout: TimeInterval = 5) -> String? {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    process.arguments = args
    let pipe = Pipe()
    process.standardOutput = pipe
    process.standardError = FileHandle.nullDevice
    do {
        try process.run()
        process.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return String(data: data, encoding: .utf8)
    } catch {
        return nil
    }
}

// ---------------------------------------------------------------------------
// MARK: - Device Listing
// ---------------------------------------------------------------------------

struct DeviceInfo: Codable, Sendable {
    let id: String
    let name: String
}

func listCameras() -> [DeviceInfo] {
    // Try v4l2-ctl --list-devices first
    if let output = shell(["v4l2-ctl", "--list-devices"]) {
        var cameras: [DeviceInfo] = []
        var currentName: String?
        for line in output.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if !line.hasPrefix("\t") && !line.hasPrefix(" ") && trimmed.hasSuffix(":") {
                currentName = String(trimmed.dropLast())
            } else if trimmed.hasPrefix("/dev/video") {
                cameras.append(DeviceInfo(id: trimmed, name: currentName ?? trimmed))
            }
        }
        if !cameras.isEmpty { return cameras }
    }

    // Fallback: scan /dev/video*
    var cameras: [DeviceInfo] = []
    for i in 0..<16 {
        let path = "/dev/video\(i)"
        if FileManager.default.fileExists(atPath: path) {
            cameras.append(DeviceInfo(id: path, name: "Camera \(i)"))
        }
    }
    return cameras
}

func listAlsaDevices(command: String) -> [DeviceInfo] {
    guard let output = shell(command.components(separatedBy: " ")) else { return [] }
    var devices: [DeviceInfo] = []
    for line in output.components(separatedBy: "\n") {
        guard line.hasPrefix("card ") else { continue }
        let parts = line.components(separatedBy: ":")
        guard parts.count >= 2 else { continue }
        let cardNum = line.components(separatedBy: " ")[1].replacingOccurrences(of: ":", with: "")
        let name = parts[1].components(separatedBy: "[")[0].trimmingCharacters(in: .whitespaces)
        devices.append(DeviceInfo(id: "hw:\(cardNum),0", name: name))
    }
    return devices
}

// ---------------------------------------------------------------------------
// MARK: - JPEG Frame Parser
// ---------------------------------------------------------------------------

struct JPEGFrameParser: Sendable {
    private var buffer = Data()

    mutating func append(_ data: Data) -> [Data] {
        buffer.append(data)
        var frames: [Data] = []
        while let range = findFrame() {
            frames.append(Data(buffer[range]))
            buffer.removeSubrange(buffer.startIndex...range.upperBound)
        }
        // Prevent unbounded growth
        if buffer.count > 10_000_000 { buffer.removeAll() }
        return frames
    }

    private func findFrame() -> ClosedRange<Int>? {
        guard buffer.count >= 4 else { return nil }
        var soi: Int?
        for i in buffer.startIndex..<(buffer.endIndex - 1) {
            if buffer[i] == 0xFF && buffer[i + 1] == 0xD8 { soi = i; break }
        }
        guard let start = soi else { return nil }
        for i in (start + 2)..<(buffer.endIndex - 1) {
            if buffer[i] == 0xFF && buffer[i + 1] == 0xD9 { return start...(i + 1) }
        }
        return nil
    }
}

// ---------------------------------------------------------------------------
// MARK: - MJPEGCamera Actor (singleton, GStreamer subprocess)
// ---------------------------------------------------------------------------

actor MJPEGCamera {
    private var subscribers: [ObjectIdentifier: @Sendable (Data) async -> Void] = [:]
    private var pipelineTask: Task<Void, any Error>?
    private var currentDevice: String

    init(device: String = "/dev/video0") {
        self.currentDevice = device
    }

    func subscribe(id: ObjectIdentifier, handler: @escaping @Sendable (Data) async -> Void) {
        subscribers[id] = handler
        if subscribers.count == 1 { startPipeline() }
    }

    func unsubscribe(id: ObjectIdentifier) {
        subscribers.removeValue(forKey: id)
        if subscribers.isEmpty { stopPipeline() }
    }

    func switchCamera(to device: String) {
        guard device != currentDevice else { return }
        currentDevice = device
        if !subscribers.isEmpty {
            stopPipeline()
            startPipeline()
        }
    }

    private func broadcast(_ frame: Data) async {
        for (_, handler) in subscribers { await handler(frame) }
    }

    private func startPipeline() {
        let device = currentDevice
        pipelineTask = Task { [weak self] in
            guard let self else { return }
            do {
                try await self.runPipeline(device: device)
            } catch is CancellationError {
                // normal
            } catch {
                print("[MJPEGCamera] pipeline error: \(error)")
            }
        }
    }

    private func stopPipeline() {
        pipelineTask?.cancel()
        pipelineTask = nil
    }

    private func runPipeline(device: String) async throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/gst-launch-1.0")
        process.arguments = [
            "v4l2src", "device=\(device)", "!",
            "image/jpeg", "!",
            "fdsink", "fd=1",
        ]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        try process.run()

        let handle = pipe.fileHandleForReading
        var parser = JPEGFrameParser()

        await withTaskCancellationHandler {
            while !Task.isCancelled {
                let chunk = handle.availableData
                if chunk.isEmpty { break }
                let frames = parser.append(chunk)
                for frame in frames { await self.broadcast(frame) }
            }
            process.terminate()
        } onCancel: {
            process.terminate()
        }
    }
}

// ---------------------------------------------------------------------------
// MARK: - AudioCapture Actor (singleton, GStreamer subprocess)
// ---------------------------------------------------------------------------

actor AudioCapture {
    private var subscribers: [ObjectIdentifier: @Sendable (Data) async -> Void] = [:]
    private var pipelineTask: Task<Void, any Error>?
    private var currentDevice: String?

    func subscribe(id: ObjectIdentifier, handler: @escaping @Sendable (Data) async -> Void) {
        subscribers[id] = handler
        if subscribers.count == 1 { startPipeline() }
    }

    func unsubscribe(id: ObjectIdentifier) {
        subscribers.removeValue(forKey: id)
        if subscribers.isEmpty { stopPipeline() }
    }

    func switchMicrophone(to device: String) {
        currentDevice = device
        if !subscribers.isEmpty {
            stopPipeline()
            startPipeline()
        }
    }

    private func broadcast(_ chunk: Data) async {
        for (_, handler) in subscribers { await handler(chunk) }
    }

    private func startPipeline() {
        let device = currentDevice
        pipelineTask = Task { [weak self] in
            guard let self else { return }
            do {
                try await self.runPipeline(device: device)
            } catch is CancellationError {
                // normal
            } catch {
                print("[AudioCapture] pipeline error: \(error)")
            }
        }
    }

    private func stopPipeline() {
        pipelineTask?.cancel()
        pipelineTask = nil
    }

    private func runPipeline(device: String?) async throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/gst-launch-1.0")

        var args: [String]
        if let device {
            args = [
                "alsasrc", "device=\(device)", "!",
                "audioconvert", "!",
                "audioresample", "!",
                "audio/x-raw,format=S16LE,channels=1,rate=16000", "!",
                "fdsink", "fd=1",
            ]
        } else {
            args = [
                "autoaudiosrc", "!",
                "audioconvert", "!",
                "audioresample", "!",
                "audio/x-raw,format=S16LE,channels=1,rate=16000", "!",
                "fdsink", "fd=1",
            ]
        }
        process.arguments = args

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        try process.run()

        let handle = pipe.fileHandleForReading

        await withTaskCancellationHandler {
            while !Task.isCancelled {
                let chunk = handle.availableData
                if chunk.isEmpty { break }
                await self.broadcast(chunk)
            }
            process.terminate()
        } onCancel: {
            process.terminate()
        }
    }
}

// ---------------------------------------------------------------------------
// MARK: - GPU Info
// ---------------------------------------------------------------------------

struct GPUInfo: Codable, Sendable {
    let available: Bool
    var name: String?
    var memory: String?
    var driver: String?
    var temperature: String?
}

func gpuInfo() -> GPUInfo {
    // Try nvidia-smi
    if let output = shell([
        "nvidia-smi",
        "--query-gpu=name,memory.total,driver_version,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]) {
        let trimmed = output.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty {
            let parts = trimmed.components(separatedBy: ",").map { $0.trimmingCharacters(in: .whitespaces) }
            return GPUInfo(
                available: true,
                name: parts.count > 0 ? parts[0] : nil,
                memory: parts.count > 1 ? "\(parts[1]) MiB" : nil,
                driver: parts.count > 2 ? parts[2] : nil,
                temperature: parts.count > 3 ? "\(parts[3])\u{00B0}C" : nil
            )
        }
    }

    // Fallback: thermal zone
    let thermalPath = "/sys/class/thermal/thermal_zone0/temp"
    if let tempStr = try? String(contentsOfFile: thermalPath, encoding: .utf8)
        .trimmingCharacters(in: .whitespacesAndNewlines),
       let tempInt = Int(tempStr)
    {
        let celsius = Double(tempInt) / 1000.0
        return GPUInfo(
            available: true,
            name: "ARM GPU",
            temperature: String(format: "%.1f\u{00B0}C", celsius)
        )
    }

    return GPUInfo(available: false)
}

// ---------------------------------------------------------------------------
// MARK: - System Info
// ---------------------------------------------------------------------------

struct SystemInfo: Codable, Sendable {
    let hostname: String
    let platform: String
    let architecture: String
    let uptime: String
    let memory: MemoryInfo
    let disk: DiskInfo
    let cpu: CPUInfo
}

struct MemoryInfo: Codable, Sendable {
    var total: String?
    var free: String?
    var used: String?
}

struct DiskInfo: Codable, Sendable {
    var total: String?
    var used: String?
    var free: String?
}

struct CPUInfo: Codable, Sendable {
    var model: String?
    var cores: Int?
}

func systemInfo() -> SystemInfo {
    let hostname = ProcessInfo.processInfo.environment["WENDY_HOSTNAME"]
        ?? ProcessInfo.processInfo.hostName

    // Memory from /proc/meminfo
    var mem = MemoryInfo()
    if let content = try? String(contentsOfFile: "/proc/meminfo", encoding: .utf8) {
        var totalMB: Int?
        var freeMB: Int?
        for line in content.components(separatedBy: "\n") {
            if line.hasPrefix("MemTotal") {
                let parts = line.components(separatedBy: .whitespaces).filter { !$0.isEmpty }
                if parts.count >= 2, let kb = Int(parts[1]) { totalMB = kb / 1024; mem.total = "\(totalMB!) MB" }
            } else if line.hasPrefix("MemAvailable") {
                let parts = line.components(separatedBy: .whitespaces).filter { !$0.isEmpty }
                if parts.count >= 2, let kb = Int(parts[1]) { freeMB = kb / 1024; mem.free = "\(freeMB!) MB" }
            }
        }
        if let t = totalMB, let f = freeMB { mem.used = "\(t - f) MB" }
    }

    // Disk
    var disk = DiskInfo()
    if let attrs = try? FileManager.default.attributesOfFileSystem(forPath: "/") {
        if let totalBytes = attrs[.systemSize] as? Int64 {
            disk.total = "\(totalBytes / (1024 * 1024 * 1024)) GB"
        }
        if let freeBytes = attrs[.systemFreeSize] as? Int64 {
            disk.free = "\(freeBytes / (1024 * 1024 * 1024)) GB"
        }
        if let t = attrs[.systemSize] as? Int64, let f = attrs[.systemFreeSize] as? Int64 {
            disk.used = "\((t - f) / (1024 * 1024 * 1024)) GB"
        }
    }

    // CPU
    var cpu = CPUInfo(cores: ProcessInfo.processInfo.processorCount)
    if let content = try? String(contentsOfFile: "/proc/cpuinfo", encoding: .utf8) {
        for line in content.components(separatedBy: "\n") {
            if line.hasPrefix("model name") {
                let parts = line.components(separatedBy: ":")
                if parts.count >= 2 {
                    cpu.model = parts[1].trimmingCharacters(in: .whitespaces)
                    break
                }
            }
        }
    }
    if cpu.model == nil {
        #if arch(arm64)
        cpu.model = "aarch64"
        #else
        cpu.model = "unknown"
        #endif
    }

    // Uptime
    var uptimeStr = ""
    if let content = try? String(contentsOfFile: "/proc/uptime", encoding: .utf8) {
        let parts = content.components(separatedBy: " ")
        if let secs = Double(parts[0]) {
            let h = Int(secs) / 3600
            let m = (Int(secs) % 3600) / 60
            uptimeStr = "\(h)h \(m)m"
        }
    }

    #if os(Linux)
    let platformStr = "Linux"
    #elseif os(macOS)
    let platformStr = "Darwin"
    #else
    let platformStr = "Unknown"
    #endif

    #if arch(arm64)
    let arch = "aarch64"
    #elseif arch(x86_64)
    let arch = "x86_64"
    #else
    let arch = "unknown"
    #endif

    return SystemInfo(
        hostname: hostname,
        platform: platformStr,
        architecture: arch,
        uptime: uptimeStr,
        memory: mem,
        disk: disk,
        cpu: cpu
    )
}

// ---------------------------------------------------------------------------
// MARK: - Content-Type Detection
// ---------------------------------------------------------------------------

func contentType(for path: String) -> String {
    let ext = (path as NSString).pathExtension.lowercased()
    switch ext {
    case "html":                    return "text/html; charset=utf-8"
    case "css":                     return "text/css; charset=utf-8"
    case "js", "mjs":               return "application/javascript; charset=utf-8"
    case "json":                    return "application/json"
    case "png":                     return "image/png"
    case "jpg", "jpeg":             return "image/jpeg"
    case "gif":                     return "image/gif"
    case "svg":                     return "image/svg+xml"
    case "ico":                     return "image/x-icon"
    case "woff":                    return "font/woff"
    case "woff2":                   return "font/woff2"
    case "ttf":                     return "font/ttf"
    case "wav":                     return "audio/wav"
    case "mp3":                     return "audio/mpeg"
    case "ogg":                     return "audio/ogg"
    case "mp4":                     return "video/mp4"
    case "webm":                    return "video/webm"
    case "webp":                    return "image/webp"
    case "txt":                     return "text/plain; charset=utf-8"
    case "xml":                     return "application/xml"
    case "pdf":                     return "application/pdf"
    case "wasm":                    return "application/wasm"
    case "map":                     return "application/json"
    default:                        return "application/octet-stream"
    }
}

// ---------------------------------------------------------------------------
// MARK: - WebSocket Message Types
// ---------------------------------------------------------------------------

private struct SwitchCameraMessage: Decodable {
    let switch_camera: String
}

private struct SwitchMicrophoneMessage: Decodable {
    let switch_microphone: String
}

// ---------------------------------------------------------------------------
// MARK: - Application Entry Point
// ---------------------------------------------------------------------------

let camera = MJPEGCamera(device: "/dev/video0")
let audio = AudioCapture()

let carStore: CarStore
do {
    carStore = try CarStore(path: "/data/cars.db")
} catch {
    fatalError("Failed to initialize database: \(error)")
}

let staticDir = "./static"

// -- HTTP Router --

let router = Router()

// MARK: Cars CRUD

let carsGroup = router.group("api/cars")

carsGroup.get { _, _ -> Response in
    let cars = try await carStore.all()
    let data = try JSONEncoder().encode(cars)
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    return Response(
        status: .ok,
        headers: [.contentType: "application/json"],
        body: .init(byteBuffer: buffer)
    )
}

carsGroup.post { request, context -> Response in
    let input = try await request.decode(as: CarInput.self, context: context)
    guard let car = try await carStore.create(input: input) else {
        throw HTTPError(.internalServerError, message: "Failed to create car")
    }
    let data = try JSONEncoder().encode(car)
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    return Response(
        status: .created,
        headers: [.contentType: "application/json"],
        body: .init(byteBuffer: buffer)
    )
}

carsGroup.get(":id") { _, context -> Response in
    guard let id = context.parameters.get("id", as: Int.self) else {
        throw HTTPError(.badRequest, message: "Invalid car ID")
    }
    guard let car = try await carStore.get(id: id) else {
        throw HTTPError(.notFound, message: "Car not found")
    }
    let data = try JSONEncoder().encode(car)
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    return Response(
        status: .ok,
        headers: [.contentType: "application/json"],
        body: .init(byteBuffer: buffer)
    )
}

carsGroup.put(":id") { request, context -> Response in
    guard let id = context.parameters.get("id", as: Int.self) else {
        throw HTTPError(.badRequest, message: "Invalid car ID")
    }
    let input = try await request.decode(as: CarInput.self, context: context)
    guard let car = try await carStore.update(id: id, input: input) else {
        throw HTTPError(.notFound, message: "Car not found")
    }
    let data = try JSONEncoder().encode(car)
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    return Response(
        status: .ok,
        headers: [.contentType: "application/json"],
        body: .init(byteBuffer: buffer)
    )
}

carsGroup.delete(":id") { _, context -> HTTPResponse.Status in
    guard let id = context.parameters.get("id", as: Int.self) else {
        throw HTTPError(.badRequest, message: "Invalid car ID")
    }
    guard try await carStore.delete(id: id) else {
        throw HTTPError(.notFound, message: "Car not found")
    }
    return .noContent
}

// MARK: Device Endpoints

router.get("api/cameras") { _, _ -> Response in
    let cameras = listCameras()
    let data = try JSONEncoder().encode(cameras)
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    return Response(
        status: .ok,
        headers: [.contentType: "application/json"],
        body: .init(byteBuffer: buffer)
    )
}

router.get("api/microphones") { _, _ -> Response in
    let mics = listAlsaDevices(command: "arecord -l")
    let data = try JSONEncoder().encode(mics)
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    return Response(
        status: .ok,
        headers: [.contentType: "application/json"],
        body: .init(byteBuffer: buffer)
    )
}

router.get("api/speakers") { _, _ -> Response in
    let speakers = listAlsaDevices(command: "aplay -l")
    let data = try JSONEncoder().encode(speakers)
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    return Response(
        status: .ok,
        headers: [.contentType: "application/json"],
        body: .init(byteBuffer: buffer)
    )
}

// MARK: GPU

router.get("api/gpu") { _, _ -> Response in
    let info = gpuInfo()
    let data = try JSONEncoder().encode(info)
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    return Response(
        status: .ok,
        headers: [.contentType: "application/json"],
        body: .init(byteBuffer: buffer)
    )
}

// MARK: System

router.get("api/system") { _, _ -> Response in
    let info = systemInfo()
    let data = try JSONEncoder().encode(info)
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    return Response(
        status: .ok,
        headers: [.contentType: "application/json"],
        body: .init(byteBuffer: buffer)
    )
}

// MARK: SPA Static File Serving

router.get("{path+}") { request, _ -> Response in
    let reqPath = request.uri.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    let filePath = "\(staticDir)/\(reqPath)"

    if FileManager.default.fileExists(atPath: filePath) {
        var isDir: ObjCBool = false
        _ = FileManager.default.fileExists(atPath: filePath, isDirectory: &isDir)
        if !isDir.boolValue {
            let data = try Data(contentsOf: URL(fileURLWithPath: filePath))
            var buffer = ByteBuffer()
            buffer.writeBytes(data)
            return Response(
                status: .ok,
                headers: [.contentType: contentType(for: filePath)],
                body: .init(byteBuffer: buffer)
            )
        }
    }

    // SPA fallback: serve index.html
    let indexPath = "\(staticDir)/index.html"
    guard let data = FileManager.default.contents(atPath: indexPath) else {
        return Response(status: .notFound, body: .init(byteBuffer: .init(string: "Not Found")))
    }
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    return Response(
        status: .ok,
        headers: [.contentType: "text/html; charset=utf-8"],
        body: .init(byteBuffer: buffer)
    )
}

// -- WebSocket Router --

let wsRouter = Router(context: BasicWebSocketRequestContext.self)

// MARK: Camera WebSocket

wsRouter.ws("api/camera/stream") { inbound, outbound, _ in
    final class ConnectionID: Sendable {}
    let connID = ConnectionID()
    let id = ObjectIdentifier(connID)

    await camera.subscribe(id: id) { frame in
        var buffer = ByteBuffer()
        buffer.writeBytes(frame)
        try? await outbound.write(.binary(buffer))
    }

    for try await message in inbound.messages(maxSize: 1_048_576) {
        if case .text(let text) = message {
            if let data = text.data(using: .utf8),
               let cmd = try? JSONDecoder().decode(SwitchCameraMessage.self, from: data)
            {
                await camera.switchCamera(to: cmd.switch_camera)
            }
        }
    }

    await camera.unsubscribe(id: id)
}

// MARK: Audio WebSocket

wsRouter.ws("api/audio/stream") { inbound, outbound, _ in
    final class ConnectionID: Sendable {}
    let connID = ConnectionID()
    let id = ObjectIdentifier(connID)

    await audio.subscribe(id: id) { chunk in
        var buffer = ByteBufferAllocator().buffer(capacity: chunk.count)
        buffer.writeBytes(chunk)
        try? await outbound.write(.binary(buffer))
    }

    for try await message in inbound.messages(maxSize: 1_048_576) {
        if case .text(let text) = message {
            if let data = text.data(using: .utf8),
               let cmd = try? JSONDecoder().decode(SwitchMicrophoneMessage.self, from: data)
            {
                await audio.switchMicrophone(to: cmd.switch_microphone)
            }
        }
    }

    await audio.unsubscribe(id: id)
}

// -- Start Application --

let app = Application(
    router: router,
    server: .http1WebSocketUpgrade(webSocketRouter: wsRouter),
    configuration: .init(
        address: .hostname("0.0.0.0", port: {{.PORT}})
    )
)

let hostDisplay = ProcessInfo.processInfo.environment["WENDY_HOSTNAME"] ?? "0.0.0.0"
print("Starting server on http://\(hostDisplay):{{.PORT}}")
try await app.runService()
