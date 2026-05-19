internal import Foundation

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

    var mem = MemoryInfo()
    if let content = try? String(contentsOfFile: "/proc/meminfo", encoding: .utf8) {
        var totalMB: Int?
        var freeMB: Int?
        for line in content.components(separatedBy: "\n") {
            if line.hasPrefix("MemTotal") {
                let parts = line.components(separatedBy: .whitespaces).filter { !$0.isEmpty }
                if parts.count >= 2, let kb = Int(parts[1]) {
                    totalMB = kb / 1024
                    mem.total = "\(kb / 1024) MB"
                }
            } else if line.hasPrefix("MemAvailable") {
                let parts = line.components(separatedBy: .whitespaces).filter { !$0.isEmpty }
                if parts.count >= 2, let kb = Int(parts[1]) {
                    freeMB = kb / 1024
                    mem.free = "\(kb / 1024) MB"
                }
            }
        }
        if let t = totalMB, let f = freeMB { mem.used = "\(t - f) MB" }
    }

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

    var uptimeStr = ""
    if let content = try? String(contentsOfFile: "/proc/uptime", encoding: .utf8),
       let secs = Double(content.components(separatedBy: " ")[0])
    {
        uptimeStr = "\(Int(secs) / 3600)h \((Int(secs) % 3600) / 60)m"
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
