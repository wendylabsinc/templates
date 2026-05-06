// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "{{.APP_ID}}",
    targets: [
        .executableTarget(name: "{{.APP_ID}}"),
    ]
)
