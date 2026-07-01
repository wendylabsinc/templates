// swift-tools-version: 6.3

import PackageDescription

let package = Package(
    name: "talker",
    platforms: [
        .macOS(.v14)
    ],
    dependencies: [
        .package(url: "https://github.com/youtalk/swift-ros2.git", from: "1.2.0"),
    ],
    targets: [
        .executableTarget(
            name: "talker",
            dependencies: [
                .product(name: "SwiftROS2", package: "swift-ros2"),
            ]
        )
    ]
)
