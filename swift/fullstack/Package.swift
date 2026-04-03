// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "{{.APP_ID}}",
    platforms: [
        .macOS(.v14),
    ],
    dependencies: [
        .package(url: "https://github.com/hummingbird-project/hummingbird.git", from: "2.21.1", traits: []),
        .package(url: "https://github.com/hummingbird-project/hummingbird-websocket.git", from: "2.0.0"),
        .package(url: "https://github.com/wendylabsinc/gstreamer-swift.git", branch: "main"),
        .package(url: "https://github.com/apple/swift-container-plugin.git", from: "1.0.0"),
    ],
    targets: [
        .executableTarget(
            name: "{{.APP_ID}}",
            dependencies: [
                .product(name: "Hummingbird", package: "hummingbird"),
                .product(name: "HummingbirdWebSocket", package: "hummingbird-websocket"),
            ],
            path: "Sources/fullstack"
        ),
    ]
)
