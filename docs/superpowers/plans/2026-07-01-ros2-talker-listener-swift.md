# ros2-talker-listener (Swift) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Swift ROS 2 `talker`/`listener` template to the Wendy templates repo, packaged as a two-service app group using swift-ros2 over CycloneDDS.

**Architecture:** One template directory `swift/ros2-talker-listener/` holding two independent, host-networked services (`talker`, `listener`), each its own SwiftPM package + Dockerfile. Both depend on the pure-Swift [swift-ros2](https://github.com/youtalk/swift-ros2) client (`SwiftROS2` product) and speak ROS 2 Humble's wire format over CycloneDDS multicast. The talker publishes `std_msgs/String` on `/chatter` at 1 Hz; the listener subscribes and logs each message. Payoff is `wendy device logs`.

**Tech Stack:** Swift 6.3, swift-ros2 1.2.0 (`SwiftROS2` product, built-in `StringMsg`), Eclipse CycloneDDS 0.10.5 (built from source in the Docker builder — provides the `CycloneDDS` pkg-config module swift-ros2 needs on Linux), Docker multi-stage (`swift:6.3-bookworm` → `swift:6.3-bookworm-slim`), Wendy templates conventions.

## Global Constraints

- Swift toolchain version: **6.3** (`.swift-version` = `6.3`, `// swift-tools-version: 6.3`), matching every other Swift template.
- swift-ros2 dependency: `.package(url: "https://github.com/youtalk/swift-ros2.git", from: "1.2.0")`, product `.product(name: "SwiftROS2", package: "swift-ros2")`.
- ROS 2 distro / wire format: **Humble** — `distro: .humble` on every `ROS2Context`.
- Transport: **`.ddsMulticast(domainId: {{.ROS_DOMAIN_ID}})`** — no Zenoh, no router.
- Message type: built-in **`StringMsg`** (`std_msgs/msg/String`); **no** `swift-ros2-gen` plugin.
- Template tokens: `{{.APP_ID}}`, `{{.ROS_DOMAIN_ID}}` (substituted into all text files, including `.swift`, exactly as `{{.PORT}}` is in `swift/simple-api`).
- Dockerfiles **COPY `Package.swift` only** — never `Package.resolved` (repo `.gitignore` ignores it repo-wide; `swift build` re-resolves). Follows `swift/simple-api` and `swift/camera-feed`.
- New template must be registered in root `meta.json` and documented in root `README.md`.

---

### Task 1: Talker service (package, node, Dockerfile)

**Files:**
- Create: `swift/ros2-talker-listener/talker/.swift-version`
- Create: `swift/ros2-talker-listener/talker/Package.swift`
- Create: `swift/ros2-talker-listener/talker/Sources/talker/main.swift`
- Create: `swift/ros2-talker-listener/talker/Dockerfile`

**Interfaces:**
- Consumes: swift-ros2 `SwiftROS2` product — `ROS2Context(transport:distro:)`, `.ddsMulticast(domainId:)`, `.humble`, `createNode(name:)`, `createPublisher(_:topic:)`, `StringMsg(data:)`, `publish(_:)`, `shutdown()`.
- Produces: a container whose entrypoint is the `talker` executable, publishing `StringMsg` on topic `chatter`. Task 3's `wendy.json` references this dir as service context `./talker`.

- [ ] **Step 1: Write `.swift-version`**

File `swift/ros2-talker-listener/talker/.swift-version`:
```
6.3
```

- [ ] **Step 2: Write `Package.swift`**

File `swift/ros2-talker-listener/talker/Package.swift`:
```swift
// swift-tools-version: 6.3

import PackageDescription

let package = Package(
    name: "talker",
    platforms: [
        .macOS(.v14)
    ],
    dependencies: [
        .package(url: "https://github.com/youtalk/swift-ros2.git", from: "1.2.0"),
    ],
    targets: [
        .executableTarget(
            name: "talker",
            dependencies: [
                .product(name: "SwiftROS2", package: "swift-ros2"),
            ]
        )
    ]
)
```

- [ ] **Step 3: Write the node `main.swift`**

File `swift/ros2-talker-listener/talker/Sources/talker/main.swift`:
```swift
// std_msgs/String publisher on /chatter at 1 Hz — the Swift half of the ROS 2
// demo_nodes talker/listener pair, over CycloneDDS (ROS 2 Humble wire format).
import Foundation
import SwiftROS2

let ctx = try await ROS2Context(
    transport: .ddsMulticast(domainId: {{.ROS_DOMAIN_ID}}),
    distro: .humble
)
let node = try await ctx.createNode(name: "talker")
let pub = try await node.createPublisher(StringMsg.self, topic: "chatter")

var count = 0
while !Task.isCancelled {
    count += 1
    let msg = StringMsg(data: "Hello World: \(count)")
    try pub.publish(msg)
    print("Publishing: '\(msg.data)'")
    try await Task.sleep(nanoseconds: 1_000_000_000)
}

await ctx.shutdown()
```

- [ ] **Step 4: Write the `Dockerfile`**

File `swift/ros2-talker-listener/talker/Dockerfile`:
```dockerfile
# syntax=docker/dockerfile:1.6
# Stage 1: build the Swift node. Eclipse CycloneDDS is built from source because
# swift-ros2 resolves CycloneDDS through pkg-config on Linux (systemLibrary
# pkgConfig: "CycloneDDS"), and the ROS apt packages target Ubuntu, not Debian.
FROM swift:6.3-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
      cmake git build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch 0.10.5 \
      https://github.com/eclipse-cyclonedds/cyclonedds.git /tmp/cyclonedds \
 && cmake -S /tmp/cyclonedds -B /tmp/cyclonedds/build \
      -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_IDLC=OFF -DENABLE_SHM=OFF -DBUILD_TESTING=OFF -DBUILD_EXAMPLES=OFF \
 && cmake --build /tmp/cyclonedds/build --target install -j"$(nproc)" \
 && rm -rf /tmp/cyclonedds

ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig
ENV LD_LIBRARY_PATH=/usr/local/lib

WORKDIR /app
COPY Package.swift ./
COPY Sources Sources
RUN --mount=type=cache,id=swiftpm-{{.APP_ID}}-talker,target=/app/.build \
    swift build -c release \
 && cp .build/release/talker /app/talker

# Stage 2: slim runtime with just the CycloneDDS shared library + the binary.
FROM swift:6.3-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /usr/local/lib/libddsc.so* /usr/local/lib/
RUN ldconfig
COPY --from=builder /app/talker /usr/local/bin/talker
CMD ["talker"]
```

- [ ] **Step 5: Verify the tree exists and files are non-empty**

Run: `find swift/ros2-talker-listener/talker -type f | sort && wc -l swift/ros2-talker-listener/talker/Sources/talker/main.swift`
Expected: the four files listed; `main.swift` has ~25 lines.

- [ ] **Step 6: Commit**

```bash
git add swift/ros2-talker-listener/talker
git commit -m "feat: add talker service for swift ros2-talker-listener template"
```

---

### Task 2: Listener service (package, node, Dockerfile)

**Files:**
- Create: `swift/ros2-talker-listener/listener/.swift-version`
- Create: `swift/ros2-talker-listener/listener/Package.swift`
- Create: `swift/ros2-talker-listener/listener/Sources/listener/main.swift`
- Create: `swift/ros2-talker-listener/listener/Dockerfile`

**Interfaces:**
- Consumes: swift-ros2 `SwiftROS2` product — additionally `createSubscription(_:topic:)` returning an object with an `AsyncSequence` `messages` property yielding `StringMsg`.
- Produces: a container whose entrypoint is the `listener` executable, subscribing to topic `chatter`. Task 3's `wendy.json` references this dir as service context `./listener`.

- [ ] **Step 1: Write `.swift-version`**

File `swift/ros2-talker-listener/listener/.swift-version`:
```
6.3
```

- [ ] **Step 2: Write `Package.swift`**

File `swift/ros2-talker-listener/listener/Package.swift`:
```swift
// swift-tools-version: 6.3

import PackageDescription

let package = Package(
    name: "listener",
    platforms: [
        .macOS(.v14)
    ],
    dependencies: [
        .package(url: "https://github.com/youtalk/swift-ros2.git", from: "1.2.0"),
    ],
    targets: [
        .executableTarget(
            name: "listener",
            dependencies: [
                .product(name: "SwiftROS2", package: "swift-ros2"),
            ]
        )
    ]
)
```

- [ ] **Step 3: Write the node `main.swift`**

File `swift/ros2-talker-listener/listener/Sources/listener/main.swift`:
```swift
// std_msgs/String subscriber on /chatter — the Swift half of the ROS 2
// demo_nodes talker/listener pair, over CycloneDDS (ROS 2 Humble wire format).
import Foundation
import SwiftROS2

let ctx = try await ROS2Context(
    transport: .ddsMulticast(domainId: {{.ROS_DOMAIN_ID}}),
    distro: .humble
)
let node = try await ctx.createNode(name: "listener")
let sub = try await node.createSubscription(StringMsg.self, topic: "chatter")

print("Listening on /chatter...")
for await msg in sub.messages {
    print("I heard: '\(msg.data)'")
}

await ctx.shutdown()
```

- [ ] **Step 4: Write the `Dockerfile`**

File `swift/ros2-talker-listener/listener/Dockerfile` — identical to the talker's except the binary name is `listener`:
```dockerfile
# syntax=docker/dockerfile:1.6
# Stage 1: build the Swift node. Eclipse CycloneDDS is built from source because
# swift-ros2 resolves CycloneDDS through pkg-config on Linux (systemLibrary
# pkgConfig: "CycloneDDS"), and the ROS apt packages target Ubuntu, not Debian.
FROM swift:6.3-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
      cmake git build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch 0.10.5 \
      https://github.com/eclipse-cyclonedds/cyclonedds.git /tmp/cyclonedds \
 && cmake -S /tmp/cyclonedds -B /tmp/cyclonedds/build \
      -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_IDLC=OFF -DENABLE_SHM=OFF -DBUILD_TESTING=OFF -DBUILD_EXAMPLES=OFF \
 && cmake --build /tmp/cyclonedds/build --target install -j"$(nproc)" \
 && rm -rf /tmp/cyclonedds

ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig
ENV LD_LIBRARY_PATH=/usr/local/lib

WORKDIR /app
COPY Package.swift ./
COPY Sources Sources
RUN --mount=type=cache,id=swiftpm-{{.APP_ID}}-listener,target=/app/.build \
    swift build -c release \
 && cp .build/release/listener /app/listener

# Stage 2: slim runtime with just the CycloneDDS shared library + the binary.
FROM swift:6.3-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /usr/local/lib/libddsc.so* /usr/local/lib/
RUN ldconfig
COPY --from=builder /app/listener /usr/local/bin/listener
CMD ["listener"]
```

- [ ] **Step 5: Verify the tree**

Run: `find swift/ros2-talker-listener/listener -type f | sort`
Expected: the four listener files.

- [ ] **Step 6: Commit**

```bash
git add swift/ros2-talker-listener/listener
git commit -m "feat: add listener service for swift ros2-talker-listener template"
```

---

### Task 3: App-group config + template metadata + template README

**Files:**
- Create: `swift/ros2-talker-listener/wendy.json`
- Create: `swift/ros2-talker-listener/template.json`
- Create: `swift/ros2-talker-listener/README.md`

**Interfaces:**
- Consumes: the two service directories `./talker` and `./listener` from Tasks 1 & 2.
- Produces: a complete, renderable Wendy template that `wendy init --template ros2-talker-listener --language swift` can scaffold.

- [ ] **Step 1: Write `wendy.json`**

File `swift/ros2-talker-listener/wendy.json`:
```json
{
    "appId": "{{.APP_ID}}",
    "version": "0.1.0",
    "platform": "linux",
    "services": {
        "talker": {
            "context": "./talker",
            "entitlements": [
                { "type": "network", "mode": "host" }
            ]
        },
        "listener": {
            "context": "./listener",
            "dependsOn": ["talker"],
            "entitlements": [
                { "type": "network", "mode": "host" }
            ]
        }
    }
}
```

- [ ] **Step 2: Write `template.json`**

File `swift/ros2-talker-listener/template.json`:
```json
{
    "name": "ros2-talker-listener",
    "description": "ROS 2 talker/listener demo in Swift (swift-ros2 over CycloneDDS): a std_msgs/String publisher and subscriber as a two-service app group",
    "variables": [
        {
            "name": "APP_ID",
            "description": "Application identifier",
            "type": "string",
            "required": true,
            "prompt": "App ID"
        },
        {
            "name": "ROS_DOMAIN_ID",
            "description": "ROS 2 domain ID for CycloneDDS discovery — talker and listener share it, and it must match any peer ROS 2 node you want to interoperate with",
            "type": "integer",
            "default": 0,
            "prompt": "ROS domain ID",
            "validate": { "min": 0, "max": 232 }
        }
    ]
}
```

- [ ] **Step 3: Write the template `README.md`**

File `swift/ros2-talker-listener/README.md`:
```markdown
# ros2-talker-listener (Swift)

The canonical ROS 2 `talker` / `listener` demo, in Swift. A two-service app
group built on [swift-ros2](https://github.com/youtalk/swift-ros2) — a pure-Swift
ROS 2 client (no `rclcpp`, no C++ interop) that speaks the ROS 2 wire format
directly over CycloneDDS.

- **talker** publishes `std_msgs/String` (`"Hello World: N"`) on `/chatter` at 1 Hz.
- **listener** subscribes to `/chatter` and logs every message it receives.

Both nodes use the **Humble** wire format over **CycloneDDS multicast**, so they
join the same ROS 2 graph as the other Humble-based Wendy templates.

## Deploy

```sh
wendy run --device <device> -y --detach
```

## See it work

```sh
wendy device logs --device <device>
```

You should see the talker's `Publishing: 'Hello World: N'` and the listener's
`I heard: 'Hello World: N'` interleaved — two separate containers discovering
each other over real DDS multicast.

## Interoperate with ROS 2

Because the wire format is Humble + CycloneDDS, a ROS 2 Humble node on the same
LAN and `ROS_DOMAIN_ID` can talk to these nodes directly:

```sh
source /opt/ros/humble/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_DOMAIN_ID=0
ros2 topic echo /chatter std_msgs/msg/String   # sees the Swift talker
ros2 run demo_nodes_cpp talker                  # the Swift listener hears it
```

## Configuration

| Variable        | Default | Purpose                                                        |
|-----------------|---------|----------------------------------------------------------------|
| `APP_ID`        | —       | Application identifier.                                         |
| `ROS_DOMAIN_ID` | `0`     | CycloneDDS discovery domain. Both services (and any ROS 2 peer) must share it. |
```

- [ ] **Step 4: Validate all JSON parses**

Run: `python3 -c "import json; [json.load(open(f)) for f in ['swift/ros2-talker-listener/wendy.json','swift/ros2-talker-listener/template.json']]" && echo OK`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add swift/ros2-talker-listener/wendy.json swift/ros2-talker-listener/template.json swift/ros2-talker-listener/README.md
git commit -m "feat: add app-group config, metadata, and README for ros2-talker-listener"
```

---

### Task 4: Register the template in `meta.json` and root `README.md`

**Files:**
- Modify: `meta.json` (append to the `templates` array, after the `rc-car` entry)
- Modify: `README.md` (add a template section)

**Interfaces:**
- Consumes: the template dir from Tasks 1–3.
- Produces: discoverability — `wendy init` lists the template; the repo README documents it.

- [ ] **Step 1: Add the `meta.json` entry**

In `meta.json`, inside the `templates` array, after the `go2-rosbag` object (the last entry), add:
```json
        {
            "name": "ros2-talker-listener",
            "description": "ROS 2 talker/listener demo in Swift (swift-ros2 over CycloneDDS): std_msgs/String publisher + subscriber as a two-service app group",
            "languages": ["swift"]
        }
```
(Add a comma after the previous last entry's closing brace so the array stays valid.)

- [ ] **Step 2: Verify `meta.json` still parses**

Run: `python3 -c "import json; d=json.load(open('meta.json')); print([t['name'] for t in d['templates']][-1])"`
Expected: `ros2-talker-listener`

- [ ] **Step 3: Add a section to root `README.md`**

In `README.md`, add after the `### realsense-camera` section (before `### audio`):
```markdown
### ros2-talker-listener

The canonical ROS 2 `talker` / `listener` demo in Swift, built on
[swift-ros2](https://github.com/youtalk/swift-ros2) — a pure-Swift ROS 2 client
that speaks the ROS 2 (Humble) wire format directly over CycloneDDS, with no
`rclcpp` or C++ interop. A two-service app group: a `std_msgs/String` publisher
on `/chatter` and a subscriber that logs what it hears.

| Language | Framework | Directory |
|----------|-----------|-----------|
| Swift | swift-ros2 1.2.0 + CycloneDDS (ROS 2 Humble) | `swift/ros2-talker-listener/` |

Deploy with `wendy run` and watch the exchange via `wendy device logs`.
```

- [ ] **Step 4: Commit**

```bash
git add meta.json README.md
git commit -m "docs: register ros2-talker-listener template in meta.json and README"
```

---

### Task 5: Render + build validation (the real test)

This task validates that the template actually builds and runs. Templates can't
build in place (they hold `{{.APP_ID}}` / `{{.ROS_DOMAIN_ID}}` tokens), so render
a copy into the scratchpad, substitute tokens, and `docker build` both services.
**Expect to iterate on the Dockerfile here** — CycloneDDS provisioning and the
swift-ros2 Linux build are the two risks. Fix issues in the real template files
(under `swift/ros2-talker-listener/`), then re-render and rebuild.

**Scratchpad:** `/private/tmp/claude-501/-Users-joannisorlandos-git-wendy-templates/4d73db9c-eeff-4791-94a8-0feea7931c95/scratchpad`

- [ ] **Step 1: Render the template into the scratchpad with tokens substituted**

Run (renders from the committed, tracked files — faithful to what `wendy init` ships):
```bash
SCRATCH="/private/tmp/claude-501/-Users-joannisorlandos-git-wendy-templates/4d73db9c-eeff-4791-94a8-0feea7931c95/scratchpad"
DEST="$SCRATCH/ros2-render"
rm -rf "$DEST" && mkdir -p "$DEST"
git archive HEAD:swift/ros2-talker-listener | tar -x -C "$DEST"
grep -rl -e '{{.APP_ID}}' -e '{{.ROS_DOMAIN_ID}}' "$DEST" | while read -r f; do
  sed -i '' -e 's/{{\.APP_ID}}/ros2demo/g' -e 's/{{\.ROS_DOMAIN_ID}}/0/g' "$f"
done
echo "--- rendered talker main.swift ---"; cat "$DEST/talker/Sources/talker/main.swift"
```
Expected: `main.swift` shows `domainId: 0` and no remaining `{{` tokens anywhere.

- [ ] **Step 2: Build the talker image**

Run:
```bash
docker build -t ros2demo-talker "$DEST/talker"
```
Expected: image builds. **If it fails**, the likely culprits and fixes:
- *pkg-config can't find CycloneDDS* → confirm `PKG_CONFIG_PATH=/usr/local/lib/pkgconfig` and that the CycloneDDS `install` step produced `/usr/local/lib/pkgconfig/CycloneDDS.pc` (add `RUN pkg-config --exists CycloneDDS && echo found` to debug).
- *swift-ros2 wants git submodules (`vendor/cyclonedds`)* → on Linux it should use the system CycloneDDS via pkg-config and not need them; if a header include fails, add `-Xcc -I/usr/local/include` via a `swift build` flag or a `.unsafeFlags` cSetting, and note it in the Dockerfile.
- *link error `-lddsc`* → ensure `ENABLE_SHM=OFF` build installed `libddsc.so` into `/usr/local/lib` and `LD_LIBRARY_PATH` includes it.

- [ ] **Step 3: Build the listener image**

Run:
```bash
docker build -t ros2demo-listener "$DEST/listener"
```
Expected: image builds.

- [ ] **Step 4: Smoke-test message flow in a shared network namespace**

Cross-container DDS multicast is unreliable on macOS Docker's default networking,
so run the listener **inside the talker's network namespace** (loopback multicast,
deterministic on one host):
```bash
docker rm -f ros2-talker ros2-listener 2>/dev/null
docker run -d --name ros2-talker ros2demo-talker
docker run -d --name ros2-listener --network container:ros2-talker ros2demo-listener
sleep 8
echo "=== talker ==="; docker logs ros2-talker | tail -5
echo "=== listener ==="; docker logs ros2-listener | tail -5
docker rm -f ros2-talker ros2-listener
```
Expected: talker logs `Publishing: 'Hello World: N'`; listener logs `Listening on /chatter...` then `I heard: 'Hello World: N'`.
**If the listener hears nothing** but both ran cleanly: multicast on `lo` may be disabled in the container. Set a permissive CycloneDDS config for the smoke test only — `docker run -e CYCLONEDDS_URI='<CycloneDDS><Domain><General><Interfaces><NetworkInterface name="lo" multicast="true"/></Interfaces></General></Domain></CycloneDDS>' ...` on both — and re-run. This is a test-harness knob, **not** a change to the template (on a real device/LAN, host networking handles discovery). If it still won't flow locally, record that and rely on the on-device check in Step 5.

- [ ] **Step 5 (authoritative, if a device is available): deploy to a device**

Render via `wendy init` and deploy for the real cross-container, host-networked check:
```bash
# in a scratch dir
wendy init --app-id ros2demo --template ros2-talker-listener --language swift --var ROS_DOMAIN_ID=0
cd ros2demo && wendy run --device <device> -y --detach
wendy device logs --device <device>   # expect interleaved Publishing:/I heard:
```
Expected: both services deploy; logs show messages flowing between the two containers.

- [ ] **Step 6: If the Dockerfile changed during iteration, commit the fixes**

```bash
git add swift/ros2-talker-listener
git commit -m "fix: make ros2-talker-listener services build and run"
```

---

### Task 6: Update memory + open the PR

**Files:**
- Create/update: memory file capturing swift-ros2 template gotchas (only the non-obvious findings from Task 5).

- [ ] **Step 1: Record what was non-obvious**

If Task 5 surfaced non-obvious build facts (e.g., the exact CycloneDDS branch that gives Humble wire compat, submodule/pkg-config quirks, whether local multicast needed a config knob), write a memory file `swift-ros2-template-gotchas.md` under the memory dir and add a one-line pointer to `MEMORY.md`. Link `[[swift-cxx-interop-template-gotchas]]`. Skip if nothing non-obvious came up.

- [ ] **Step 2: Push the branch**

```bash
git push -u origin feat/ros2-talker-listener-swift
```

- [ ] **Step 3: Open the PR**

```bash
gh pr create --base main --title "Add ROS 2 talker/listener template in Swift" --body "$(cat <<'EOF'
## Summary

Adds `swift/ros2-talker-listener` — the canonical ROS 2 `talker`/`listener` demo in Swift, as a two-service app group.

- Uses [swift-ros2](https://github.com/youtalk/swift-ros2) (pure-Swift ROS 2 client, no `rclcpp`/C++ interop) over CycloneDDS, ROS 2 Humble wire format.
- **talker**: publishes `std_msgs/String` on `/chatter` at 1 Hz. **listener**: subscribes and logs each message.
- Wire-compatible with the repo's existing Humble-based ROS 2 templates.
- Registered in `meta.json` and documented in the root `README.md`.

Design + plan: `docs/superpowers/specs/2026-07-01-ros2-talker-listener-swift-design.md`, `docs/superpowers/plans/2026-07-01-ros2-talker-listener-swift.md`.

## Validation

Rendered the template, `docker build` both services, and confirmed `Publishing:` / `I heard:` message flow. [Update with actual results, incl. on-device if run.]

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01VQ3DosZiiBU97p3YN1HwxQ
EOF
)"
```
Expected: PR URL printed.

---

## Self-Review

**Spec coverage:**
- Binding approach (swift-ros2, DDS multicast, Humble, StringMsg, no codegen) → Global Constraints + Tasks 1–2.
- Two-service packaging / directory structure → Tasks 1, 2, 3.
- Node code (talker/listener) → Tasks 1.3, 2.3.
- wendy.json (host networking, dependsOn) → Task 3.1.
- Dockerfile (CycloneDDS from source, multi-stage, COPY Package.swift only) → Tasks 1.4, 2.4.
- Repo integration (meta.json, README) → Task 4.
- Validation (render from tracked files, docker build both, observe flow) → Task 5.
- PR → Task 6.
All spec sections map to a task. No gaps.

**Placeholder scan:** No TBD/TODO/"add error handling" — every file's full contents are inline. The one bracketed note (`[Update with actual results…]`) is an intentional instruction to fill the PR body with real validation output, not a code placeholder.

**Type consistency:** `ROS2Context(transport:distro:)`, `.ddsMulticast(domainId:)`, `.humble`, `createNode(name:)`, `createPublisher(_:topic:)`, `createSubscription(_:topic:)`, `.messages`, `StringMsg(data:)`, `publish(_:)`, `shutdown()` — used identically in Tasks 1 and 2 and match the upstream swift-ros2 examples. Executable/target names `talker`/`listener` match their Dockerfile `cp`/`CMD` and the `wendy.json` service contexts. Token names `{{.APP_ID}}`/`{{.ROS_DOMAIN_ID}}` consistent across all files and the Task 5 render.
