
public final class AVSourceDiscovery: Sendable {

    public struct SourceInfo: Codable, Sendable {
        public let id: String
        public let name: String
        public let host: String
        public let port: Int
    }

    public static let shared = AVSourceDiscovery()
    
    private init() {}
    
    public func listCameras() -> [SourceInfo] {
        return [
            SourceInfo(
                id: "wlite-av-src:0",
                name: "WendyLite Camera at 192.168.1.206",
                host: "192.168.1.206",
                port: 3333
            ),
        ]
    }
}
