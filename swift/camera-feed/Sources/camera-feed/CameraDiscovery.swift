internal import Foundation

struct CameraInfo: Codable, Sendable {
    let id: String
    let name: String
}

func listCameras() -> [CameraInfo] {
    let cameras = listCamerasViaV4L2()
    if !cameras.isEmpty { return cameras }
    return listCamerasViaScan()
}

private func listCamerasViaV4L2() -> [CameraInfo] {
    let process = Process()
    process.executableURL = URL(filePath: "/usr/bin/v4l2-ctl")
    process.arguments = ["--list-devices"]

    let pipe = Pipe()
    process.standardOutput = pipe
    process.standardError = FileHandle.nullDevice

    do {
        try process.run()
        process.waitUntilExit()
    } catch {
        return []
    }

    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    guard let output = String(data: data, encoding: .utf8) else { return [] }

    var cameras: [CameraInfo] = []
    var currentName: String?

    for line in output.components(separatedBy: "\n") {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        if !line.hasPrefix("\t") && !line.hasPrefix(" ") && trimmed.hasSuffix(":") {
            currentName = String(trimmed.dropLast())
        } else if trimmed.hasPrefix("/dev/video") {
            cameras.append(CameraInfo(id: trimmed, name: currentName ?? trimmed))
        }
    }

    return cameras
}

private func listCamerasViaScan() -> [CameraInfo] {
    let fm = FileManager.default
    var cameras: [CameraInfo] = []

    for i in 0..<16 {
        let path = "/dev/video\(i)"
        if fm.fileExists(atPath: path) {
            cameras.append(CameraInfo(id: path, name: "Camera \(i)"))
        }
    }

    return cameras
}
