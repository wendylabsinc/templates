// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "llm-chat-server",
    platforms: [
        .macOS(.v14)
    ],
    dependencies: [
        .package(url: "https://github.com/hummingbird-project/hummingbird.git", from: "2.21.1", traits: []),
        .package(url: "https://github.com/apple/swift-container-plugin", from: "1.0.0"),
{{ if eq .BACKEND "mlx" }}
        .package(url: "https://github.com/ml-explore/mlx-swift-lm.git", branch: "main"),
{{ end }}
    ],
    targets: [
{{ if eq .BACKEND "mlx" }}
        .executableTarget(
            name: "llm-server-mlx",
            dependencies: [
                .product(name: "Hummingbird", package: "hummingbird"),
                .product(name: "MLXLLM", package: "mlx-swift-lm"),
                .product(name: "MLXLMCommon", package: "mlx-swift-lm"),
            ]
        )
{{ else }}
        .executableTarget(
            name: "llm-server-gguf",
            dependencies: [
                .product(name: "Hummingbird", package: "hummingbird")
            ]
        )
{{ end }}
    ]
)
