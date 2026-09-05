// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "{{.APP_ID}}",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(url: "https://github.com/hummingbird-project/hummingbird.git", from: "2.21.1", traits: []),
    ],
    targets: [
        .executableTarget(name: "{{.APP_ID}}", dependencies: [
            .product(name: "Hummingbird", package: "hummingbird"),
        ]),
    ]
)
