internal import Foundation

struct GPUInfo: Codable, Sendable {
    let available: Bool
    var name: String?
    var memory: String?
    var driver: String?
    var temperature: String?
}

func gpuInfo() -> GPUInfo {
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

    if let tempStr = try? String(contentsOfFile: "/sys/class/thermal/thermal_zone0/temp", encoding: .utf8)
        .trimmingCharacters(in: .whitespacesAndNewlines),
       let tempInt = Int(tempStr)
    {
        return GPUInfo(
            available: true,
            name: "ARM GPU",
            temperature: String(format: "%.1f\u{00B0}C", Double(tempInt) / 1000.0)
        )
    }

    return GPUInfo(available: false)
}
