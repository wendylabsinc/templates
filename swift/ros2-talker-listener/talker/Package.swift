// swift-tools-version: 6.3

import PackageDescription

let package = Package(
    name: "talker",
    platforms: [
        .macOS(.v14)
    ],
    dependencies: [
        .package(url: "https://github.com/youtalk/swift-ros2.git", from: "1.2.0"),
        .package(url: "https://github.com/swift-otel/swift-otel.git", from: "1.0.0", traits: ["OTLPHTTP", "OTLPGRPC"]),
    ],
    targets: [
        .executableTarget(
            name: "talker",
            dependencies: [
                .product(name: "SwiftROS2", package: "swift-ros2"),
                .product(name: "OTel", package: "swift-otel"),
            ]
        )
    ]
)
