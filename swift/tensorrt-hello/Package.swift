// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "tensorrt-hello",
    platforms: [
        .macOS(.v14)
    ],
    dependencies: [
        .package(url: "https://github.com/wendylabsinc/tensorrt-swift.git", from: "0.0.4"),
        .package(url: "https://github.com/apple/swift-container-plugin", from: "1.0.0"),
    ],
    targets: [
        .executableTarget(
            name: "tensorrt-hello",
            dependencies: [
                .product(name: "TensorRT", package: "tensorrt-swift")
            ]
        )
    ]
)
