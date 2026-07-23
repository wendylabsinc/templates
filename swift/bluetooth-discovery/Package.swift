// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "bluetooth-discovery",
    platforms: [
        .macOS(.v15)
    ],
    dependencies: [
        .package(url: "https://github.com/hummingbird-project/hummingbird.git", from: "2.21.1", traits: []),
        .package(url: "https://github.com/wendylabsinc/bluetooth.git", from: "0.0.2"),
        .package(url: "https://github.com/apple/swift-container-plugin", from: "1.0.0"),
    ],
    targets: [
        .executableTarget(
            name: "BluetoothDiscovery",
            dependencies: [
                .product(name: "Hummingbird", package: "hummingbird"),
                .product(name: "Bluetooth", package: "bluetooth"),
            ],
            path: "Sources/BluetoothDiscovery"
        )
    ]
)
