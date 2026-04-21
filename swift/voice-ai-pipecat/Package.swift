// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "{{.APP_ID}}",
    platforms: [
        .macOS(.v14)
    ],
    dependencies: [
        .package(url: "https://github.com/pvieito/PythonKit.git", branch: "master"),
    ],
    targets: [
        .executableTarget(
            name: "{{.APP_ID}}",
            dependencies: [
                .product(name: "PythonKit", package: "PythonKit")
            ]
        )
    ]
)
