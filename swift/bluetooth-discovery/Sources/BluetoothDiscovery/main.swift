import Bluetooth
import Foundation
import Hummingbird

struct DiscoveredDevice: Codable, ResponseEncodable, Sendable {
    let name: String?
    let address: String
    let rssi: Int
}

private func address(of result: ScanResult) -> String {
    let raw = result.peripheral.id.rawValue
    return raw.hasPrefix("addr:") ? String(raw.dropFirst("addr:".count)) : raw
}

private func device(from result: ScanResult) -> DiscoveredDevice {
    DiscoveredDevice(
        name: result.peripheral.name ?? result.advertisementData.localName,
        address: address(of: result),
        rssi: result.rssi
    )
}

// Collect unique devices seen during a bounded scan window.
private func scan(seconds: Double) async throws -> [DiscoveredDevice] {
    let manager = CentralManager(options: BluetoothOptions())
    let stream = try await manager.scan(filter: nil, parameters: ScanParameters(allowDuplicates: false))
    let deadline = ContinuousClock.now.advanced(by: .seconds(seconds))
    let collector = Task<[DiscoveredDevice], Error> {
        var seen: [String: DiscoveredDevice] = [:]
        for try await result in stream {
            let d = device(from: result)
            seen[d.address] = d
        }
        return Array(seen.values)
    }
    try? await Task.sleep(until: deadline, clock: .continuous)
    try? await manager.stopScan()
    collector.cancel()
    return (try? await collector.value) ?? []
}

let router = Router()

router.get("/") { _, _ -> Response in
    let html = try String(contentsOfFile: "index.html", encoding: .utf8)
    return Response(
        status: .ok,
        headers: [.contentType: "text/html"],
        body: .init(byteBuffer: ByteBuffer(string: html))
    )
}

router.get("/logo.svg") { _, _ -> Response in
    let svg = try String(contentsOfFile: "logo.svg", encoding: .utf8)
    return Response(
        status: .ok,
        headers: [.contentType: "image/svg+xml"],
        body: .init(byteBuffer: ByteBuffer(string: svg))
    )
}

router.get("/discovered") { _, _ -> [DiscoveredDevice] in
    try await scan(seconds: 5.0)
}

// SSE: stream devices as they are discovered for ~30s.
router.get("/events") { _, _ -> Response in
    let body = ResponseBody { writer in
        let manager = CentralManager(options: BluetoothOptions())
        let stream = try await manager.scan(filter: nil, parameters: ScanParameters(allowDuplicates: false))
        let deadline = ContinuousClock.now.advanced(by: .seconds(30))
        let encoder = JSONEncoder()
        do {
            for try await result in stream {
                if ContinuousClock.now >= deadline { break }
                let data = try encoder.encode(device(from: result))
                var buf = ByteBuffer()
                buf.writeString("data: ")
                buf.writeBytes(data)
                buf.writeString("\n\n")
                try await writer.write(buf)
            }
        } catch {}
        try? await manager.stopScan()
        try await writer.finish(nil)
    }
    return Response(
        status: .ok,
        headers: [.contentType: "text/event-stream", .cacheControl: "no-cache"],
        body: body
    )
}

let port = {{.PORT}}
let app = Application(
    router: router,
    configuration: .init(address: .hostname("0.0.0.0", port: port))
)
try await app.runService()
