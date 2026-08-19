import Foundation
import Logging

// ───────────────────────────────────────────────────────────────────────────
// V4LCameraDiscovery
//
// Enumerates the V4L nodes on the box by shelling out to v4l2-ctl, then asks
// each one which pixel formats it offers to keep only the nodes that can
// actually produce frames.
// ───────────────────────────────────────────────────────────────────────────

private let logger = Logger(label: "V4LCameraDiscovery")

private let kV4L2Ctl = "/usr/bin/v4l2-ctl"

struct V4LCamera: Sendable {
    let device: String
    let name: String
    /// FourCCs as reported by VIDIOC_ENUM_FMT, e.g. ["YUYV", "MJPG"].
    let formats: [String]
}

struct V4LCameraDiscovery {
    func listCameras() -> [V4LCamera] {
        guard let output = capturedOutput(of: kV4L2Ctl, ["--list-devices"]) else { return [] }
        var nodes: [(device: String, name: String)] = []
        var currentName: String?
        for line in output.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if !line.hasPrefix("\t") && !line.hasPrefix(" ") && trimmed.hasSuffix(":") {
                currentName = String(trimmed.dropLast())
            } else if trimmed.hasPrefix("/dev/video") {
                nodes.append((device: trimmed, name: currentName ?? trimmed))
            }
        }
        return nodes.compactMap { node in
            let formats = captureFormats(of: node.device)
            guard !formats.isEmpty else {
                logger.info("[cameras] skipping \(node.device): enumerates no capture formats")
                return nil
            }
            return V4LCamera(device: node.device, name: node.name, formats: formats)
        }
    }

    // A UVC camera exposes a metadata-only node alongside the real one, and that
    // node answers VIDIOC_ENUM_FMT with an empty list. v4l2-ctl prints its
    // "Type: Video Capture" header either way, so the header proves nothing —
    // what separates the two is whether any format entry follows it:
    //
    //     [0]: 'YUYV' (YUYV 4:2:2)
    //     [1]: 'MJPG' (Motion-JPEG, compressed)
    //
    // An empty return therefore means "not a capture node". --list-formats
    // enumerates only the capture buffer type, so an entry can't come from
    // anywhere else; --list-formats-ext would add every frame size and interval
    // for the same fourccs.
    private func captureFormats(of device: String) -> [String] {
        guard let output = capturedOutput(of: kV4L2Ctl, ["-d", device, "--list-formats"])
        else { return [] }
        return output.components(separatedBy: "\n").compactMap { line in
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard trimmed.hasPrefix("["),
                  let open = trimmed.firstIndex(of: "'"),
                  let close = trimmed[trimmed.index(after: open)...].firstIndex(of: "'")
            else { return nil }
            return String(trimmed[trimmed.index(after: open)..<close])
        }
    }
}

// Run a tool and return its stdout, or nil if it couldn't be spawned. Reads
// before waiting: waitUntilExit() first deadlocks if the child fills the pipe
// buffer.
private func capturedOutput(of executable: String, _ arguments: [String]) -> String? {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: executable)
    process.arguments = arguments
    let pipe = Pipe()
    process.standardOutput = pipe
    process.standardError = FileHandle.nullDevice
    do {
        try process.run()
    } catch {
        return nil
    }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    return String(data: data, encoding: .utf8)
}
