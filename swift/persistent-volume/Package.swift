// swift-tools-version: 6.3

import PackageDescription

let package = Package(
    name: "{{.APP_ID}}",
    platforms: [
        .macOS(.v14)
    ],
    dependencies: [
        .package(url: "https://github.com/apple/swift-container-plugin", from: "1.0.0"),
    ],
    targets: [
        .executableTarget(
            name: "{{.APP_ID}}"
        )
    ]
)
