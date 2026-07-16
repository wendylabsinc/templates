// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "{{.APP_ID}}",
    platforms: [
        .macOS("26.0"),
    ],
    dependencies: [
        .package(url: "https://github.com/wendylabsinc/gstreamer-swift.git", branch: "main"),
    ],
    targets: [
        .systemLibrary(
            name: "CWhisper",
            path: "Sources/CWhisper"
        ),
        .executableTarget(
            name: "{{.APP_ID}}",
            dependencies: [
                .product(name: "GStreamer", package: "gstreamer-swift"),
                "CWhisper",
            ]
        ),
    ]
)
