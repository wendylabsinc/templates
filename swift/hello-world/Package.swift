// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "{{.APP_ID}}",
    dependencies: [
        .package(url: "https://github.com/wendylabsinc/wendy-lite", branch: "main"),
    ],
    targets: [
        .executableTarget(
            name: "{{.APP_ID}}",
            dependencies: [
                .product(name: "WendyLite", package: "wendy-lite"),
            ],
            swiftSettings: [
                .enableExperimentalFeature("Embedded"),
                .unsafeFlags(["-wmo"]),
            ],
            linkerSettings: [
                .unsafeFlags([
                    "-Xclang-linker", "-nostartfiles",
                    "-Xlinker", "--no-entry",
                    "-Xlinker", "--export=_start",
                    "-Xlinker", "--allow-undefined",
                    "-Xlinker", "--initial-memory=65536",
                    "-Xlinker", "--table-base=1",
                    "-Xlinker", "--strip-all",
                    "-Xlinker", "--export=malloc",
                    "-Xlinker", "--export=free",
                    "-Xlinker", "--export=wendy_handle_callback",
                    "-Xlinker", "-z", "-Xlinker", "stack-size=8192",
                ])
            ]
        ),
    ]
)
