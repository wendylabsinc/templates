// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "camera-feed-yolo",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(url: "https://github.com/hummingbird-project/hummingbird.git", from: "2.21.1"),
        .package(url: "https://github.com/hummingbird-project/hummingbird-websocket.git", from: "2.0.0"),
        .package(url: "https://github.com/apple/swift-container-plugin", from: "1.0.0"),
        .package(url: "https://github.com/apple/swift-log.git", from: "1.14.0"),
    ],
    targets: [
        .systemLibrary(
            name: "COnnxRuntime",
            pkgConfig: "libonnxruntime"
        ),
        .systemLibrary(
            name: "CTurboJPEG",
            pkgConfig: "libturbojpeg"
        ),
        .executableTarget(
            name: "camera-feed-yolo",
            dependencies: [
                .product(name: "Hummingbird", package: "hummingbird"),
                .product(name: "HummingbirdWebSocket", package: "hummingbird-websocket"),
                .product(name: "Logging", package: "swift-log"),
                "COnnxRuntime",
                "CTurboJPEG",
            ]
        )
    ]
)
