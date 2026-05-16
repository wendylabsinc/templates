// swift-tools-version: 6.3

import PackageDescription

let package = Package(
    name: "{{.APP_ID}}",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(url: "https://github.com/hummingbird-project/hummingbird.git", from: "2.21.1"),
        .package(url: "https://github.com/apple/swift-container-plugin", from: "1.0.0"),
    ],
    targets: [
        .target(
            name: "RealSenseBridge",
            publicHeadersPath: "include",
            cxxSettings: [
                .headerSearchPath("include"),
                .unsafeFlags(["-std=c++17", "-I/usr/local/include"]),
            ],
            linkerSettings: [
                .linkedLibrary("realsense2"),
                .linkedLibrary("turbojpeg"),
                .unsafeFlags(["-L/usr/local/lib"]),
            ]
        ),
        .executableTarget(
            name: "{{.APP_ID}}",
            dependencies: [
                .product(name: "Hummingbird", package: "hummingbird"),
                "RealSenseBridge",
            ]
        ),
    ]
)
