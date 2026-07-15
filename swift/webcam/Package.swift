// swift-tools-version: 6.2

import PackageDescription

let package = Package(
  name: "webcam-server",
  platforms: [
    .macOS("26.0")
  ],
  dependencies: [
    .package(url: "https://github.com/hummingbird-project/hummingbird.git", from: "2.21.1"),
    .package(
      url: "https://github.com/hummingbird-project/hummingbird-websocket.git", from: "2.0.0"),
    .package(url: "https://github.com/wendylabsinc/gstreamer-swift.git", from: "0.0.3"),
    .package(url: "https://github.com/apple/swift-container-plugin", from: "1.0.0"),
  ],
  targets: [
    .executableTarget(
      name: "WebcamServer",
      dependencies: [
        .product(name: "Hummingbird", package: "hummingbird"),
        .product(name: "HummingbirdWebSocket", package: "hummingbird-websocket"),
        .product(name: "GStreamer", package: "gstreamer-swift"),
      ],
      path: "Server/Sources/WebcamServer",
      swiftSettings: [
        .unsafeFlags(["-parse-as-library"])
      ]
    )
  ]
)
