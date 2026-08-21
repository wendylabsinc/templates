// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "{{.APP_ID}}",
    platforms: [.macOS(.v15)],
    dependencies: [
        .package(url: "https://github.com/hummingbird-project/hummingbird.git", from: "2.21.1"),
        .package(url: "https://github.com/hummingbird-project/hummingbird-websocket.git", from: "2.0.0"),
        .package(url: "https://github.com/apple/swift-container-plugin", from: "1.0.0"),
        .package(url: "https://github.com/apple/swift-log.git", from: "1.14.0"),
        .package(url: "https://github.com/swift-otel/swift-otel.git", from: "1.0.0", traits: ["OTLPHTTP", "OTLPGRPC"]),
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
            name: "{{.APP_ID}}",
            dependencies: [
                .product(name: "Hummingbird", package: "hummingbird"),
                .product(name: "HummingbirdWebSocket", package: "hummingbird-websocket"),
                .product(name: "Logging", package: "swift-log"),
                .product(name: "OTel", package: "swift-otel"),
                "COnnxRuntime",
                "CTurboJPEG",
            ]
        )
    ]
)
