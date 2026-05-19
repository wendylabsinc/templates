internal import Foundation

struct DeviceInfo: Codable, Sendable {
    let id: String
    let name: String
}

func shell(_ args: [String]) -> String? {
    let process = Process()
    process.executableURL = URL(filePath: "/usr/bin/env")
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

func listCameras() -> [DeviceInfo] {
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
