internal import Foundation

struct DeviceInfo: Codable, Sendable {
    let id: String
    let name: String
}

func shell(_ args: [String]) -> String? {
    let process = Process()
    // /usr/bin/env resolves the binary via PATH, which is sufficient for the
    // target embedded Linux environments (Raspberry Pi, Jetson) where these
    // tools (v4l2-ctl, arecord, aplay) are installed in standard PATH locations.
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

func v4l2DeviceName(_ path: String) -> String {
    guard let output = shell(["v4l2-ctl", "--device", path, "--info"]) else {
        return URL(filePath: path).lastPathComponent
    }
    for line in output.components(separatedBy: "\n") {
        guard line.contains("Card type"), let range = line.range(of: ":") else { continue }
        return String(line[range.upperBound...]).trimmingCharacters(in: .whitespacesAndNewlines)
    }
    return URL(filePath: path).lastPathComponent
}

func v4l2IsCaptureDevice(_ path: String) -> Bool {
    guard let output = shell(["v4l2-ctl", "--device", path, "--all"]) else { return false }
    return output.contains("Video Capture")
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
                guard v4l2IsCaptureDevice(trimmed) else { continue }
                cameras.append(DeviceInfo(id: trimmed, name: currentName ?? v4l2DeviceName(trimmed)))
            }
        }
        if !cameras.isEmpty { return cameras }
    }

    var cameras: [DeviceInfo] = []
    for i in 0..<64 {
        let path = "/dev/video\(i)"
        if FileManager.default.fileExists(atPath: path), v4l2IsCaptureDevice(path) {
            cameras.append(DeviceInfo(id: path, name: v4l2DeviceName(path)))
        }
    }
    return cameras
}

func listAlsaDevices(args: [String]) -> [DeviceInfo] {
    guard let output = shell(args) else { return [] }
    var seen = Set<String>()
    var devices: [DeviceInfo] = []
    for line in output.components(separatedBy: "\n") {
        guard line.hasPrefix("card ") else { continue }
        guard let deviceRange = line.range(of: ", device ") else { continue }
        let cardPortion = line[..<deviceRange.lowerBound]
        let devicePortion = line[deviceRange.upperBound...]

        let cardSplit = cardPortion.split(separator: ":", maxSplits: 1)
        guard cardSplit.count == 2 else { continue }
        let cardWords = cardSplit[0].split(separator: " ")
        guard cardWords.count >= 2 else { continue }
        let cardNum = String(cardWords[1])

        let deviceSplit = devicePortion.split(separator: ":", maxSplits: 1)
        guard deviceSplit.count == 2 else { continue }
        let deviceNum = deviceSplit[0].trimmingCharacters(in: .whitespacesAndNewlines)

        let id = "hw:\(cardNum),\(deviceNum)"
        guard seen.insert(id).inserted else { continue }

        let cardName = cardSplit[1]
            .split(separator: "[", maxSplits: 1)
            .first
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) } ?? ""
        let deviceName = deviceSplit[1]
            .split(separator: "[", maxSplits: 1)
            .first
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) } ?? ""

        let displayName: String
        if !cardName.isEmpty && !deviceName.isEmpty {
            displayName = "\(cardName) - \(deviceName)"
        } else {
            displayName = cardName.isEmpty ? deviceName : cardName
        }

        devices.append(DeviceInfo(id: id, name: displayName.isEmpty ? id : displayName))
    }
    return devices
}
