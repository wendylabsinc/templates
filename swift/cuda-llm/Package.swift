// swift-tools-version: 6.3

import PackageDescription

let package = Package(
    name: "cuda-llm",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(url: "https://github.com/wendylabsinc/mlx-swift-lm", branch: "gab/demo-jetson-lm-v2"),
        .package(url: "https://github.com/huggingface/swift-huggingface", from: "0.9.0"),
        .package(url: "https://github.com/huggingface/swift-transformers", from: "1.3.0"),
    ],
    targets: [
        .executableTarget(
            name: "cuda-llm",
            dependencies: [
                .product(name: "MLXLLM", package: "mlx-swift-lm"),
                .product(name: "MLXLMCommon", package: "mlx-swift-lm"),
                .product(name: "MLXHuggingFace", package: "mlx-swift-lm"),
                .product(name: "HuggingFace", package: "swift-huggingface"),
                .product(name: "Tokenizers", package: "swift-transformers")
            ]
        ),
    ]
)
