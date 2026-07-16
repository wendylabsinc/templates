# ros2-talker-listener (Swift) — Design

Date: 2026-07-01

## Goal

Add a Swift ROS 2 template to the Wendy templates repo: the canonical
`demo_nodes_cpp` **talker / listener** pair, implemented in Swift. It proves a
Swift↔ROS 2 pub/sub round-trip end-to-end and gives users a Swift on-ramp into
the ROS 2 ecosystem that already exists in this repo (the Python `go2-*` and
`rc-car` templates run ROS 2 Humble + CycloneDDS).

## Binding approach

Use **[swift-ros2](https://github.com/youtalk/swift-ros2)** via its `SwiftROS2`
umbrella product — a *pure-Swift* ROS 2 client. It speaks the ROS 2 wire format
directly over CycloneDDS; there is **no `rclcpp`/C++ interop** and no ROS 2
client-library cross-compilation.

- **Transport:** DDS / CycloneDDS multicast — `.ddsMulticast(domainId:)`.
  Peer-to-peer discovery, no Zenoh router process required. Matches the
  CycloneDDS stack the existing Python ROS 2 templates use, so the Swift nodes
  join the same ROS graph.
- **Distro / wire format:** **Humble** (`distro: .humble`). Chosen for
  wire-compatibility with the repo's existing Humble-based templates, so
  `ros2 topic echo /chatter std_msgs/msg/String` from those images sees the
  Swift talker's messages.
- **Message type:** the built-in `StringMsg` (`std_msgs/msg/String`),
  re-exported by the `SwiftROS2` umbrella. **No `swift-ros2-gen` code-gen
  plugin is needed** for this demo.
- **Package dependency:** `.package(url: "https://github.com/youtalk/swift-ros2.git", from: "1.0.0")`
  → `.product(name: "SwiftROS2", package: "swift-ros2")`. (Exact version pinned
  during build validation.)

## Packaging

A single template packaged as a **two-service app group** (mirrors
`python/go2-rc`'s multi-service structure). Two independent containers discover
each other over real DDS multicast — the faithful ROS 2 proof — rather than two
nodes sharing one process.

```
swift/ros2-talker-listener/
├── template.json          # vars: APP_ID, ROS_DOMAIN_ID (default 0)
├── wendy.json             # 2 services, both network:host; listener dependsOn talker
├── README.md
├── talker/
│   ├── .swift-version
│   ├── Package.swift      # SwiftROS2 dep; executable target "talker"
│   ├── Dockerfile         # multi-stage: builder (Swift + CycloneDDS) → slim runtime
│   └── Sources/talker/main.swift
└── listener/
    ├── .swift-version
    ├── Package.swift      # SwiftROS2 dep; executable target "listener"
    ├── Dockerfile
    └── Sources/listener/main.swift
```

Modest duplication of `Package.swift`/`Dockerfile` across the two service dirs
is intentional and idiomatic — each service is an independent build context, the
same way `go2-rc`'s `motion`/`camera`/`rc` services are.

## Node code

Adapted from the upstream `Sources/Examples/Talker` and `Listener` examples,
with the CLI transport-switching argument parsing removed and the transport
hardcoded:

- **talker:** open `ROS2Context(transport: .ddsMulticast(domainId: <ROS_DOMAIN_ID>), distro: .humble)`,
  `createNode(name: "talker")`, `createPublisher(StringMsg.self, topic: "chatter")`,
  publish `StringMsg(data: "Hello World: \(count)")` at 1 Hz, log `Publishing: '…'`.
- **listener:** same context/node, `createSubscription(StringMsg.self, topic: "chatter")`,
  `for await msg in sub.messages { print("I heard: '\(msg.data)'") }`.

`ROS_DOMAIN_ID` is templated into the source (or read from the environment) so
both services agree on the domain.

## wendy.json

```jsonc
{
  "appId": "{{.APP_ID}}",
  "version": "0.1.0",
  "platform": "linux",
  "services": {
    "talker": {
      "context": "./talker",
      "entitlements": [{ "type": "network", "mode": "host" }]
    },
    "listener": {
      "context": "./listener",
      "dependsOn": ["talker"],
      "entitlements": [{ "type": "network", "mode": "host" }]
    }
  }
}
```

Host networking so CycloneDDS multicast discovery works across the two
containers (same rationale as the go2 templates). No readiness probe and no
browser hook — this is a background pub/sub demo whose payoff is
`wendy device logs`.

## Dockerfile (primary build risk)

Multi-stage:

1. **Builder** — Swift toolchain image **plus CycloneDDS with its pkg-config
   `.pc` file** (swift-ros2's Linux build resolves CycloneDDS through
   `pkg-config`). Provision CycloneDDS via the ROS apt package
   (`ros-humble-cyclonedds`) or build Eclipse CycloneDDS from source; set
   `PKG_CONFIG_PATH` so `swift build` finds it. `COPY Package.swift` only (never
   `Package.resolved` — the repo `.gitignore` ignores it repo-wide; `swift build`
   re-resolves from the manifest).
2. **Runtime** — slim Swift runtime image carrying only the built executable and
   the CycloneDDS shared library.

This is the piece most likely to need iteration; it is validated by an actual
`docker build`, not by inspection.

## Repo integration

- Add a `meta.json` entry: `{ "name": "ros2-talker-listener", "description":
  "…", "languages": ["swift"] }`.
- Add a `## ros2-talker-listener` section to the top-level `README.md`.

## Validation

Templates cannot build in place (they contain `{{.APP_ID}}` / `{{.ROS_DOMAIN_ID}}`
tokens). Validation workflow (per the repo's C++-interop-template lesson):

1. Render the template from **tracked files only** (`git archive HEAD:<path>` or
   equivalent), not a raw `cp -R` that would pull in gitignored local files.
2. Substitute the tokens, rename `Sources/talker` → `Sources/<app-id>`-style as
   needed for each service.
3. `docker build` **both** services.
4. Ideally deploy/run the group and confirm `Publishing:` (talker) and
   `I heard:` (listener) appear in the logs, i.e. real cross-container DDS
   discovery works.

Only after a clean build + observed message flow: branch off `main` and open the
PR.

## Out of scope (YAGNI)

- Zenoh transport / `rmw_zenohd` router.
- `swift-ros2-gen` custom message generation (built-in `StringMsg` suffices).
- Non-Swift language variants of this template.
- A web UI / Foxglove bridge (that would be a separate template).
