// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "{{.APP_ID}}",
    platforms: [.macOS("26.0")],
    dependencies: [
        .package(url: "https://github.com/hummingbird-project/hummingbird.git", from: "2.21.1", traits: []),
        .package(url: "https://github.com/apple/swift-container-plugin", from: "1.0.0"),
        .package(url: "https://github.com/swift-otel/swift-otel.git", from: "1.0.0", traits: ["OTLPHTTP", "OTLPGRPC"]),
    ],
    targets: [
        .systemLibrary(name: "CRealsense2", pkgConfig: "realsense2"),
        .systemLibrary(name: "CTurboJPEG", pkgConfig: "libturbojpeg"),
        .target(
            name: "RealSenseKit",
            dependencies: ["CRealsense2"]
        ),
        .executableTarget(
            name: "{{.APP_ID}}",
            dependencies: [
                "RealSenseKit",
                "CTurboJPEG",
                .product(name: "Hummingbird", package: "hummingbird"),
                .product(name: "OTel", package: "swift-otel"),
            ],
            swiftSettings: [
                .interoperabilityMode(.Cxx)
            ]
        ),
    ],
    cxxLanguageStandard: .cxx17
)
