# Templates dedup + Swift ports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate the camera examples onto `webcam` across all five languages, merge the two Swift LLM chat templates behind a `BACKEND` variable, and add a Swift port of `bluetooth-discovery`.

**Architecture:** Three independent phases (A: bluetooth port — greenfield; B: camera consolidation — port `webcam` to cpp/rust/node then delete `camera-feed`; C: Swift LLM merge — one template with Go-`text/template` conditionals). Each phase ends with `meta.json`/`README.md` in sync and every touched template render- and build-verified.

**Tech Stack:** Wendy templates (`wendy init --template`), Go `text/template` rendering, Docker, Swift 6.2 + Hummingbird 2.21.1, `wendylabsinc/bluetooth` (BlueZ D-Bus backend), GStreamer, React/Vite (shadcn), llama.cpp / MLX-Swift.

## Global Constraints

- Templates are rendered by `wendy init --template <name> --language <lang> --app-id <id> --var KEY=VAL`. There is **no CI build gate** — every template must be verified locally by rendering then `docker build`.
- Any variable referenced in a template's `template.schema.json` MUST also appear in that template's `template.json`, or rendering emits `<no value>`.
- `meta.json` must remain valid JSON (verify with `python3 -m json.tool meta.json >/dev/null`) and its `templates[].name` set must match the on-disk template directories after every phase.
- Swift template convention for multi-file servers (follow `swift/webcam`, not `swift/simple-api`): **fixed** SwiftPM target name + explicit `path:`, binary renamed to a fixed name in the Dockerfile. Only `wendy.json`, the `EXPOSE`/`CMD` lines, and `let port = {{.PORT}}` in source use template variables.
- Swift deps pinned to match siblings: `hummingbird` from `2.21.1`, `swift-container-plugin` from `1.0.0`. Swift build base image: `swift:6.2.3-noble` (build) / `swift:6.2.3-noble-slim` (runtime).
- Per-template `wendy.json` uses `"appId": "{{.APP_ID}}"`, `"platform": "linux"`, a `tcpSocket` readiness probe on `{{.PORT}}`, and a `postStart` open-browser hook — copy the shape from the sibling being ported.
- Commit after each task with a scoped message. Work stays on branch `jo/samples-consolidation`.

---

# Phase A — Swift `bluetooth-discovery` (greenfield)

New template `swift/bluetooth-discovery`: Hummingbird server serving the existing Python template's `index.html`, with `GET /discovered` (JSON) and `GET /events` (SSE), backed by `CentralManager.scan()` from `wendylabsinc/bluetooth`. On Linux the package auto-selects its BlueZ D-Bus backend (matching the `bluetooth: bluez` entitlement, which injects a filtered `org.bluez` system-bus socket — not raw HCI).

**Reference sources to read first:** `python/bluetooth-discovery/` (endpoint contract, `index.html`, `logo.svg`, `wendy.json`), `swift/webcam/` (Dockerfile + Package.swift + `let port = {{.PORT}}` pattern), and the cloned package at the path printed by `wendylabsinc/bluetooth` `Examples/Discovery/main.swift` for the `CentralManager`/`ScanResult` API.

### Task A1: Scaffold the Swift bluetooth-discovery package (compiles, serves index)

**Files:**
- Create: `swift/bluetooth-discovery/Package.swift`
- Create: `swift/bluetooth-discovery/Sources/BluetoothDiscovery/main.swift`
- Create: `swift/bluetooth-discovery/.swift-version` (copy `swift/webcam/.gitignore`'s sibling if present; else `6.2.3`)
- Create: `swift/bluetooth-discovery/index.html` (copy from `python/bluetooth-discovery/index.html`)
- Create: `swift/bluetooth-discovery/logo.svg` (copy from `python/bluetooth-discovery/logo.svg`)

**Interfaces:**
- Produces: an executable target `BluetoothDiscovery` (binary renamed to `bluetooth-discovery` in Task A3), listening on `{{.PORT}}`, routes `GET /`, `GET /logo.svg`, `GET /discovered`, `GET /events`.
- Consumes: `wendylabsinc/bluetooth` API — `CentralManager(options:)`, `manager.scan(filter:parameters:) -> AsyncStream<ScanResult>` (throwing), `manager.stopScan()`. `ScanResult.peripheral.id.rawValue` is `"addr:<mac>"`, `ScanResult.peripheral.name: String?`, `ScanResult.advertisementData.localName: String?`, `ScanResult.rssi: Int`.

- [ ] **Step 1: Write `Package.swift`**

```swift
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
```

- [ ] **Step 2: Write `Sources/BluetoothDiscovery/main.swift`**

```swift
import Bluetooth
import Foundation
import Hummingbird

struct DiscoveredDevice: Codable, ResponseEncodable, Sendable {
    let name: String?
    let address: String
    let rssi: Int
}

private func address(of result: ScanResult) -> String {
    let raw = result.peripheral.id.rawValue
    return raw.hasPrefix("addr:") ? String(raw.dropFirst("addr:".count)) : raw
}

private func device(from result: ScanResult) -> DiscoveredDevice {
    DiscoveredDevice(
        name: result.peripheral.name ?? result.advertisementData.localName,
        address: address(of: result),
        rssi: result.rssi
    )
}

// Collect unique devices seen during a bounded scan window.
private func scan(seconds: Double) async throws -> [DiscoveredDevice] {
    let manager = CentralManager(options: BluetoothOptions())
    let stream = try await manager.scan(filter: nil, parameters: ScanParameters(allowDuplicates: false))
    var seen: [String: DiscoveredDevice] = [:]
    let deadline = ContinuousClock.now.advanced(by: .seconds(seconds))
    let collector = Task {
        for try await result in stream {
            let d = device(from: result)
            seen[d.address] = d
        }
    }
    try? await Task.sleep(until: deadline, clock: .continuous)
    try? await manager.stopScan()
    collector.cancel()
    return Array(seen.values)
}

let router = Router()

router.get("/") { _, _ -> Response in
    let html = try String(contentsOfFile: "index.html", encoding: .utf8)
    return Response(
        status: .ok,
        headers: [.contentType: "text/html"],
        body: .init(byteBuffer: ByteBuffer(string: html))
    )
}

router.get("/logo.svg") { _, _ -> Response in
    let svg = try String(contentsOfFile: "logo.svg", encoding: .utf8)
    return Response(
        status: .ok,
        headers: [.contentType: "image/svg+xml"],
        body: .init(byteBuffer: ByteBuffer(string: svg))
    )
}

router.get("/discovered") { _, _ -> [DiscoveredDevice] in
    try await scan(seconds: 5.0)
}

// SSE: stream devices as they are discovered for ~30s.
router.get("/events") { _, _ -> Response in
    let body = ResponseBody { writer in
        let manager = CentralManager(options: BluetoothOptions())
        let stream = try await manager.scan(filter: nil, parameters: ScanParameters(allowDuplicates: false))
        let deadline = ContinuousClock.now.advanced(by: .seconds(30))
        let encoder = JSONEncoder()
        do {
            for try await result in stream {
                if ContinuousClock.now >= deadline { break }
                let data = try encoder.encode(device(from: result))
                var buf = ByteBuffer()
                buf.writeString("data: ")
                buf.writeBytes(data)
                buf.writeString("\n\n")
                try await writer.write(buf)
            }
        } catch {}
        try? await manager.stopScan()
        try await writer.finish(nil)
    }
    return Response(
        status: .ok,
        headers: [.contentType: "text/event-stream", .cacheControl: "no-cache"],
        body: body
    )
}

let port = {{.PORT}}
let app = Application(
    router: router,
    configuration: .init(address: .hostname("0.0.0.0", port: port))
)
try await app.runService()
```

- [ ] **Step 3: Copy static assets and swift-version**

```bash
cp python/bluetooth-discovery/index.html swift/bluetooth-discovery/index.html
cp python/bluetooth-discovery/logo.svg swift/bluetooth-discovery/logo.svg
printf '6.2.3\n' > swift/bluetooth-discovery/.swift-version
```

- [ ] **Step 4: Verify it compiles natively (macOS uses the CoreBluetooth backend; this only checks the Swift code + routes)**

Run: `cd swift/bluetooth-discovery && swift build 2>&1 | tail -20 && cd -`
Expected: builds successfully. If the Hummingbird `ResponseBody`/`writer` or `.contentType` API differs in 2.21.x, adjust to the installed API (check `Router`/`ResponseBody` symbols) until it compiles. Note `{{.PORT}}` will be a literal `{{.PORT}}` on disk and will NOT compile in place — temporarily substitute a real port for this local check, or skip to Task A2's render+build which resolves it.

- [ ] **Step 5: Commit**

```bash
git add swift/bluetooth-discovery
git commit -m "feat(swift): scaffold bluetooth-discovery server (Hummingbird + wendylabsinc/bluetooth)"
```

### Task A2: Template manifests (`template.json`, `wendy.json`) + render check

**Files:**
- Create: `swift/bluetooth-discovery/template.json`
- Create: `swift/bluetooth-discovery/wendy.json`

**Interfaces:**
- Consumes: variables `APP_ID` (string, required) and `PORT` (integer, default 8000) — same names Phase A source/Dockerfile reference.

- [ ] **Step 1: Write `template.json`**

```json
{
    "name": "bluetooth-discovery",
    "description": "BLE scanner in Swift: live device stream (wendylabsinc/bluetooth + Hummingbird + SSE + web UI)",
    "variables": [
        {
            "name": "APP_ID",
            "description": "Application identifier",
            "type": "string",
            "required": true,
            "prompt": "App ID"
        },
        {
            "name": "PORT",
            "description": "Primary HTTP port",
            "type": "integer",
            "default": 8000,
            "prompt": "HTTP port",
            "validate": { "min": 1, "max": 65535 }
        }
    ]
}
```

- [ ] **Step 2: Write `wendy.json` (copy the Python template's entitlements verbatim)**

```json
{
    "appId": "{{.APP_ID}}",
    "version": "0.1.0",
    "platform": "linux",
    "entitlements": [
        {
            "type": "network",
            "mode": "host"
        },
        {
            "type": "bluetooth",
            "mode": "bluez"
        }
    ],
    "readiness": {
        "tcpSocket": { "port": {{.PORT}} },
        "timeoutSeconds": 30
    },
    "hooks": {
        "postStart": {
            "cli": "wendy utils open-browser http://${WENDY_HOSTNAME}:{{.PORT}}"
        }
    }
}
```

- [ ] **Step 3: Render the template and confirm no `<no value>` and valid manifests**

Run:
```bash
rm -rf /tmp/bt-render && wendy init --app-id btdisco --template bluetooth-discovery --language swift --var PORT=8000 /tmp/bt-render
grep -rn "<no value>" /tmp/bt-render && echo "RENDER BUG" || echo "render clean"
python3 -m json.tool /tmp/bt-render/wendy.json >/dev/null && echo "wendy.json valid"
grep -n "let port = 8000" /tmp/bt-render/Sources/BluetoothDiscovery/main.swift
```
Expected: "render clean", "wendy.json valid", and `let port = 8000` present. (If `wendy init`'s destination-arg form differs, use the interactive/`--help` form; the check is that rendered output has no `<no value>` and PORT resolved.)

- [ ] **Step 4: Commit**

```bash
git add swift/bluetooth-discovery/template.json swift/bluetooth-discovery/wendy.json
git commit -m "feat(swift): bluetooth-discovery template + wendy manifests"
```

### Task A3: Dockerfile (Linux BlueZ build) + image build verification

**Files:**
- Create: `swift/bluetooth-discovery/Dockerfile`

**Interfaces:**
- Consumes: target `BluetoothDiscovery`, assets `index.html`/`logo.svg`, variables `APP_ID`/`PORT`.
- Produces: an image whose entrypoint is the renamed `bluetooth-discovery` binary listening on `{{.PORT}}`, with `libdbus-1` present at runtime (the package's BlueZ backend links libdbus via `wendylabsinc/dbus`).

- [ ] **Step 1: Write `Dockerfile`** (mirrors `swift/webcam`; swap GStreamer packages for D-Bus)

```dockerfile
# Build Swift server with D-Bus (BlueZ backend of wendylabsinc/bluetooth links libdbus-1)
FROM swift:6.2.3-noble AS swift-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    libdbus-1-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY Package.swift Package.resolved* ./
COPY Sources ./Sources

RUN swift build

# Runtime stage
FROM swift:6.2.3-noble-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libdbus-1-3 \
    bluez \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=swift-builder /app/.build/debug/BluetoothDiscovery /usr/local/bin/bluetooth-discovery
COPY index.html .
COPY logo.svg .

EXPOSE {{.PORT}}

CMD ["bluetooth-discovery"]
```

- [ ] **Step 2: Build the rendered image (validates the Linux BlueZ compile end-to-end)**

Run:
```bash
cd /tmp/bt-render
cp /Users/joannisorlandos/git/wendy/templates/swift/bluetooth-discovery/Dockerfile .   # if render predates A3; else re-render
# render already substituted {{.PORT}}; if the on-disk Dockerfile still has {{.PORT}}, re-run wendy init first
docker build --platform linux/arm64 -t bt-disco-test .
cd -
```
Expected: image builds; the Swift server + `Bluetooth` (BlueZ backend) compile against `libdbus-1-dev`. Runtime BLE scanning can only be exercised on a real device with the entitlement — document that; do not block on it.

- [ ] **Step 3: Commit**

```bash
git add swift/bluetooth-discovery/Dockerfile
git commit -m "feat(swift): bluetooth-discovery Dockerfile (Linux BlueZ/libdbus build)"
```

### Task A4: Register in `meta.json` + `README.md`

**Files:**
- Modify: `meta.json` (the `bluetooth-discovery` entry)
- Modify: `README.md` (bluetooth-discovery section)

- [ ] **Step 1: Add `swift` to the `bluetooth-discovery` languages in `meta.json`**

Change that entry's `"languages"` from `["python"]` to `["python", "swift"]`.

- [ ] **Step 2: Update the README's bluetooth-discovery section**

Add a Swift row to its language table: `| Swift | Hummingbird + wendylabsinc/bluetooth | 8000 | swift/bluetooth-discovery/ |` (match the existing table's columns).

- [ ] **Step 3: Verify meta is valid + in sync**

Run: `python3 -m json.tool meta.json >/dev/null && echo ok`
Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add meta.json README.md
git commit -m "docs: register swift bluetooth-discovery in meta + README"
```

---

# Phase B — Camera consolidation (`webcam` → all 5 languages, delete `camera-feed`)

`webcam` is MJPEG-over-WebSocket via GStreamer (the "WebRTC" wording in `meta.json`/`README.md` is inaccurate). It is a superset of `camera-feed` adding: `GET /cameras` enumeration, WS `{"switch_camera": id}` switching, `GET /logs`, `GET /debug`, native-MJPEG-preferred pipeline with `jpegenc` fallback (Jetson `nvjpegenc`). Port it to cpp/rust/node by extending each language's existing `camera-feed`, then delete `camera-feed` everywhere.

**Reference sources to read first (per language):** `python/webcam/app.py` + `swift/webcam/Server/Sources/WebcamServer/main.swift` (the feature-complete references), and the target language's `cpp/camera-feed/main.cpp` / `rust/camera-feed/src/main.rs` / `node/camera-feed/src/index.ts` (the base to extend). Also `common/camera-feed-html/` (shared HTML source of truth).

### Feature parity bar (applies to each of B1–B3)
Each new `<lang>/webcam` must serve:
- `GET /` → `index.html`; `GET /logo.svg` (or `assets/…` matching that language's existing camera-feed asset convention)
- `WS /stream` → JPEG frames; accept text message `{"switch_camera": "<id>"}`
- `GET /cameras` → JSON array of `{ "id": string, "name": string }`
- GStreamer pipeline ladder identical in intent to `python/webcam` `_start_pipeline`: try `image/jpeg` (native), then `image/jpeg,width=640,height=480`, then `videoconvert ! jpegenc quality=70`, into `appsink`. Preserve any Jetson `nvjpegenc` path the language's existing `camera-feed` already used.

`GET /logs` and `GET /debug` are ported only where that language's `camera-feed` already maintains a log buffer; otherwise omit them and note it in the commit message. Use `webcam`'s `index.html` (copy from `python/webcam/index.html`, which drives `/stream` + `/cameras`).

### Task B1: `cpp/webcam`

**Files:**
- Create: `cpp/webcam/` = copy of `cpp/camera-feed/` then extend (`main.cpp`, `CMakeLists.txt`, `Dockerfile`, `template.json`, `wendy.json`, `index.html`, `assets/`)

- [ ] **Step 1: Copy the base and rename**

```bash
cp -r cpp/camera-feed cpp/webcam
```

- [ ] **Step 2: Set `template.json` name/description**

Set `"name": "webcam"` and `"description": "Generic USB/UVC webcam: GStreamer MJPEG over WebSocket with multi-camera enumeration"`. Keep the existing `APP_ID` + `PORT` variables (keep camera-feed's default port).

- [ ] **Step 3: Replace `index.html` with the webcam UI**

```bash
cp python/webcam/index.html cpp/webcam/index.html
```

- [ ] **Step 4: Extend `main.cpp` to the parity bar**

Add, to the existing camera-feed GStreamer+WS server: (a) `GET /cameras` enumerating `/dev/video*` capture devices (use `Gst.DeviceMonitor` for `Video/Source`/`video/x-raw`, fall back to scanning `/dev/video*` and reading the card name via V4L2 — mirror `python/webcam` `enumerate_cameras`/`_v4l2_device_name`); (b) handling of a `{"switch_camera": id}` WS text message that tears down and rebuilds the pipeline on the new device (mirror `switch_camera`); (c) the three-rung pipeline ladder from the parity bar. Read `python/webcam/app.py` lines 45–230 for the exact behavior to reproduce.

- [ ] **Step 5: Update `wendy.json`** — copy `swift/webcam/wendy.json` entitlements shape (`network: host`, `camera`, `gpu`), keeping `{{.APP_ID}}`/`{{.PORT}}`.

- [ ] **Step 6: Render + build**

Run:
```bash
rm -rf /tmp/webcam-cpp && wendy init --app-id webcamcpp --template webcam --language cpp --var PORT=3003 /tmp/webcam-cpp
grep -rn "<no value>" /tmp/webcam-cpp && echo "RENDER BUG" || echo "render clean"
cd /tmp/webcam-cpp && docker build --platform linux/arm64 -t webcam-cpp-test . && cd -
```
Expected: render clean, image builds.

- [ ] **Step 7: Commit**

```bash
git add cpp/webcam
git commit -m "feat(cpp): port webcam template (enumeration + switching) from camera-feed"
```

### Task B2: `rust/webcam`

**Files:**
- Create: `rust/webcam/` = copy of `rust/camera-feed/` then extend (`src/main.rs`, `Cargo.toml`, `Dockerfile`, `template.json`, `wendy.json`, `index.html`, `assets/`)

- [ ] **Step 1: Copy the base**

```bash
cp -r rust/camera-feed rust/webcam
```

- [ ] **Step 2: Set `template.json` name/description** — as in B1 Step 2.

- [ ] **Step 3: Replace `index.html`** — `cp python/webcam/index.html rust/webcam/index.html`.

- [ ] **Step 4: Extend `src/main.rs` to the parity bar** — add `/cameras` enumeration, `{"switch_camera": id}` handling, and the pipeline ladder, mirroring `python/webcam/app.py`. Update `Cargo.toml` if the enumeration needs an added crate (e.g. `gstreamer::DeviceMonitor` is already in the `gstreamer` crate the base uses — prefer it over new deps).

- [ ] **Step 5: Update `wendy.json`** — as in B1 Step 5.

- [ ] **Step 6: Render + build**

Run:
```bash
rm -rf /tmp/webcam-rust && wendy init --app-id webcamrust --template webcam --language rust --var PORT=3003 /tmp/webcam-rust
grep -rn "<no value>" /tmp/webcam-rust && echo "RENDER BUG" || echo "render clean"
cd /tmp/webcam-rust && docker build --platform linux/arm64 -t webcam-rust-test . && cd -
```
Expected: render clean, image builds.

- [ ] **Step 7: Commit**

```bash
git add rust/webcam
git commit -m "feat(rust): port webcam template (enumeration + switching) from camera-feed"
```

### Task B3: `node/webcam`

**Files:**
- Create: `node/webcam/` = copy of `node/camera-feed/` then extend (`src/index.ts`, `package.json`, `tsconfig.json`, `Dockerfile`, `template.json`, `wendy.json`, `index.html`, `assets/`)

- [ ] **Step 1: Copy the base**

```bash
cp -r node/camera-feed node/webcam
```

- [ ] **Step 2: Set `template.json` name/description** — as in B1 Step 2.

- [ ] **Step 3: Replace `index.html`** — `cp python/webcam/index.html node/webcam/index.html`.

- [ ] **Step 4: Extend `src/index.ts` to the parity bar** — add `/cameras`, `{"switch_camera": id}`, pipeline ladder, mirroring `python/webcam/app.py`. Reuse the base's GStreamer approach (bindings or spawned `gst-launch`), extending rather than replacing.

- [ ] **Step 5: Update `wendy.json`** — as in B1 Step 5.

- [ ] **Step 6: Render + build**

Run:
```bash
rm -rf /tmp/webcam-node && wendy init --app-id webcamnode --template webcam --language node --var PORT=3003 /tmp/webcam-node
grep -rn "<no value>" /tmp/webcam-node && echo "RENDER BUG" || echo "render clean"
cd /tmp/webcam-node && docker build --platform linux/arm64 -t webcam-node-test . && cd -
```
Expected: render clean, image builds.

- [ ] **Step 7: Commit**

```bash
git add node/webcam
git commit -m "feat(node): port webcam template (enumeration + switching) from camera-feed"
```

### Task B4: Delete `camera-feed` and reconcile `meta.json` + `README.md`

**Files:**
- Delete: `python/camera-feed/`, `swift/camera-feed/`, `cpp/camera-feed/`, `rust/camera-feed/`, `node/camera-feed/`
- Modify: `meta.json`, `README.md`

- [ ] **Step 1: Remove all camera-feed directories**

```bash
git rm -r python/camera-feed swift/camera-feed cpp/camera-feed rust/camera-feed node/camera-feed
```

- [ ] **Step 2: Update `meta.json`** — delete the `camera-feed` entry; on the `webcam` entry remove the `"languages"` field (so it implies all 5 languages) and set the description to `"Generic USB/UVC webcam: GStreamer MJPEG over WebSocket with multi-camera enumeration + switching"`.

- [ ] **Step 3: Update `README.md`** — remove the `camera-feed` section; fix the `webcam` section (remove the WebRTC claim; describe MJPEG-over-WebSocket + enumeration/switching) and add `cpp`/`rust`/`node` rows to its language/directory table. Check the `camera-feed-html` mention (~line 274): leave it if it refers to the `common/` asset dir, remove it if it referenced the deleted template.

- [ ] **Step 4: Verify meta valid + no dangling refs**

Run:
```bash
python3 -m json.tool meta.json >/dev/null && echo "meta ok"
grep -rn "camera-feed[^-]" README.md meta.json || echo "no stray camera-feed refs"
```
Expected: `meta ok`; only `camera-feed-yolo` (if anything) remains — no bare `camera-feed`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: drop camera-feed; webcam is the single camera template (5 languages)"
```

---

# Phase C — Merge Swift LLM chats behind a `BACKEND` variable

Replace `swift/mlx-llm-chat` + `swift/llm-gguf` with one `swift/llm-chat` selected by a `BACKEND` variable (`gguf` default, `mlx`). Go-`text/template` conditionals switch `Dockerfile` + `Package.swift`; both server source trees ship; one shared React frontend. Fix the two known defects: MLX-Swift is Apple-only (document `mlx` as macOS-only, `gguf` as the Linux/Jetson default) and `llama.cpp.tar.gz` is untracked (fetch llama.cpp via a pinned `git clone` in the Dockerfile instead of `COPY`).

**Reference sources to read first:** `swift/llm-gguf/` (Dockerfile with the llama.cpp CUDA build; `server/Sources/qwen3-chat-server`; `frontend/`) and `swift/mlx-llm-chat/` (Dockerfile; `server/Sources/mlx-llm-server`; `frontend/`). Diff the two `frontend/src/App.tsx` to choose the kept frontend.

**Verification reality:** neither backend image builds on this macOS host (gguf needs a Jetson CUDA base; mlx-swift needs Apple/Metal and cannot build in the Linux container at all). So Phase C's gate is: (1) `wendy init` renders cleanly for BOTH `BACKEND=gguf` and `BACKEND=mlx` (valid JSON, no `<no value>`, `Package.swift` parses), and (2) `swift build` of the **gguf** server target (plain Hummingbird, no special deps) succeeds natively. Full image builds are deferred to a device and must be noted as such.

### Task C1: Assemble the merged template skeleton (shared frontend + both server targets)

**Files:**
- Create: `swift/llm-chat/frontend/` (copy the chosen frontend)
- Create: `swift/llm-chat/server/Sources/llm-server-gguf/` (copy from `swift/llm-gguf/server/Sources/qwen3-chat-server`)
- Create: `swift/llm-chat/server/Sources/llm-server-mlx/` (copy from `swift/mlx-llm-chat/server/Sources/mlx-llm-server`)

**Interfaces:**
- Produces: two SwiftPM targets, `llm-server-gguf` and `llm-server-mlx`, each an independent executable. Task C2's `Package.swift` conditionally declares one of them per `BACKEND`.

- [ ] **Step 1: Create dirs and copy the two server source trees**

```bash
mkdir -p swift/llm-chat/server/Sources
cp -r swift/llm-gguf/server/Sources/qwen3-chat-server swift/llm-chat/server/Sources/llm-server-gguf
cp -r swift/mlx-llm-chat/server/Sources/mlx-llm-server swift/llm-chat/server/Sources/llm-server-mlx
```

- [ ] **Step 2: Choose and copy the frontend** — diff `swift/llm-gguf/frontend/src/App.tsx` vs `swift/mlx-llm-chat/frontend/src/App.tsx`; copy the `llm-gguf` frontend (the working Jetson chat) unless the diff shows the mlx one is clearly better. `cp -r swift/llm-gguf/frontend swift/llm-chat/frontend`.

- [ ] **Step 3: Commit**

```bash
git add swift/llm-chat
git commit -m "feat(swift): assemble llm-chat skeleton (both server targets + shared frontend)"
```

### Task C2: Conditional `Package.swift` + `template.json` + `template.schema.json`

**Files:**
- Create: `swift/llm-chat/server/Package.swift`
- Create: `swift/llm-chat/template.json`
- Create: `swift/llm-chat/template.schema.json`

**Interfaces:**
- Consumes: variable `BACKEND` ∈ `{"gguf","mlx"}` (default `gguf`), plus `APP_ID`, `PORT`.
- Produces: a package that declares exactly one executable target (`llm-server-gguf` or `llm-server-mlx`) per the rendered `BACKEND`.

- [ ] **Step 1: Write conditional `server/Package.swift`**

```swift
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
```

- [ ] **Step 2: Write `template.json`** (BACKEND must live here, not only in the schema)

```json
{
    "name": "llm-chat",
    "description": "Swift LLM chat (Hummingbird + React): llama.cpp/GGUF on Jetson or MLX-Swift on Apple Silicon",
    "variables": [
        {
            "name": "APP_ID",
            "description": "Application identifier",
            "type": "string",
            "required": true,
            "prompt": "App ID"
        },
        {
            "name": "PORT",
            "description": "Primary HTTP port",
            "type": "integer",
            "default": 6002,
            "prompt": "HTTP port",
            "validate": { "min": 1, "max": 65535 }
        },
        {
            "name": "BACKEND",
            "description": "Inference backend",
            "type": "string",
            "default": "gguf",
            "prompt": "Inference backend (gguf=Jetson/llama.cpp, mlx=Apple Silicon)",
            "validate": { "enum": ["gguf", "mlx"] }
        }
    ]
}
```

- [ ] **Step 2b: Write `template.schema.json`** (wizard question mirroring BACKEND; format follows `python/llm/template.schema.json`)

```json
{
    "phases": [
        {
            "id": "backend",
            "title": "Inference backend",
            "questions": [
                {
                    "id": "BACKEND",
                    "label": "Choose the inference backend",
                    "type": "radio",
                    "required": true,
                    "default": "gguf",
                    "options": [
                        { "value": "gguf", "label": "llama.cpp (GGUF)", "comments": "Jetson/CUDA. Default." },
                        { "value": "mlx", "label": "MLX-Swift", "comments": "Apple Silicon only (Metal); not for Linux/Jetson." }
                    ]
                }
            ]
        }
    ]
}
```

- [ ] **Step 3: Render both backends and confirm the correct target is declared**

Run:
```bash
for b in gguf mlx; do
  rm -rf /tmp/llm-$b && wendy init --app-id llm$b --template llm-chat --language swift --var PORT=6002 --var BACKEND=$b /tmp/llm-$b
  echo "== $b =="; grep -rn "<no value>" /tmp/llm-$b && echo "RENDER BUG" || echo "render clean"
  grep -n "executableTarget" /tmp/llm-$b/server/Package.swift
done
```
Expected: both render clean; `gguf` declares `llm-server-gguf` and no mlx dep; `mlx` declares `llm-server-mlx` + the mlx dep.

- [ ] **Step 4: `swift build` the gguf server target natively (validates the kept server code)**

Run: `cd /tmp/llm-gguf/server && swift build 2>&1 | tail -20 && cd -`
Expected: builds (plain Hummingbird). Fix any target-name/path drift from the copy in Task C1.

- [ ] **Step 5: Commit**

```bash
git add swift/llm-chat/server/Package.swift swift/llm-chat/template.json swift/llm-chat/template.schema.json
git commit -m "feat(swift): llm-chat BACKEND-conditional Package.swift + template manifests"
```

### Task C3: Conditional `Dockerfile` (gguf fetches llama.cpp; mlx builds MLX) + `wendy.json`

**Files:**
- Create: `swift/llm-chat/Dockerfile`
- Create: `swift/llm-chat/wendy.json`

- [ ] **Step 1: Write the conditional `Dockerfile`** — one frontend build stage shared, then a `{{ if eq .BACKEND "gguf" }} … {{ else }} … {{ end }}` split for the server/runtime stages. The `gguf` branch reproduces `swift/llm-gguf/Dockerfile` but **replaces** `COPY llama.cpp.tar.gz` + untar with a pinned fetch:

```dockerfile
ARG LLAMA_CPP_REF=b4000
RUN git clone --depth 1 --branch ${LLAMA_CPP_REF} https://github.com/ggerganov/llama.cpp /opt/llama.cpp \
    && cmake -S /opt/llama.cpp -B /opt/llama.cpp/build -G Ninja \
        -DGGML_CUDA=ON -DGGML_CUDA_NO_VMM=ON -DGGML_CURL=ON \
        -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH} \
        -DCMAKE_SHARED_LINKER_FLAGS="-L/usr/local/cuda/lib64/stubs -lcuda" \
        -DCMAKE_EXE_LINKER_FLAGS="-L/usr/local/cuda/lib64/stubs -lcuda" \
        -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /opt/llama.cpp/build --target llama-cli -j
```

Keep the rest of the `gguf` branch (base `dustynv/tensorrt:8.6-r36.2.0`, `ARG CUDA_ARCH=87`, Swift toolchain install, `swift build`, runtime copies, the `QWEN_*` env vars, binary `llm-server-gguf`). The `else` (mlx) branch reproduces `swift/mlx-llm-chat/Dockerfile` (base `swift:6.2.3-noble`, binary `llm-server-mlx`, `MODEL_ID` env). Both branches end `EXPOSE {{.PORT}}` and `CMD ["llm-server-<backend>"]`. Pin `LLAMA_CPP_REF` to a real release tag verified during implementation.

- [ ] **Step 2: Write `wendy.json`** (copy `swift/llm-gguf/wendy.json`; entitlements are backend-independent)

Use `appId: {{.APP_ID}}`, `platform: linux`, entitlements `network: host` + `gpu`, `readiness.tcpSocket` on `{{.PORT}}`, `postStart` open-browser hook. (Copy the exact shape from `swift/llm-gguf/wendy.json`.)

- [ ] **Step 3: Render both backends; confirm each Dockerfile picks the right stage**

Run:
```bash
for b in gguf mlx; do
  rm -rf /tmp/llm-$b && wendy init --app-id llm$b --template llm-chat --language swift --var PORT=6002 --var BACKEND=$b /tmp/llm-$b
  echo "== $b =="; grep -n "dustynv/tensorrt\|swift:6.2.3-noble\|llm-server-$b\|llama.cpp" /tmp/llm-$b/Dockerfile | head
done
```
Expected: `gguf` Dockerfile references `dustynv/tensorrt` + `git clone … llama.cpp` + `llm-server-gguf`; `mlx` references `swift:6.2.3-noble` + `llm-server-mlx` and no llama.cpp.

- [ ] **Step 4: Commit**

```bash
git add swift/llm-chat/Dockerfile swift/llm-chat/wendy.json
git commit -m "feat(swift): llm-chat BACKEND-conditional Dockerfile (pinned llama.cpp fetch) + wendy.json"
```

### Task C4: Remove old templates + reconcile `meta.json` / `README.md`

**Files:**
- Delete: `swift/mlx-llm-chat/`, `swift/llm-gguf/`
- Modify: `meta.json`, `README.md`

- [ ] **Step 1: Remove the two old Swift templates**

```bash
git rm -r swift/mlx-llm-chat swift/llm-gguf
```

- [ ] **Step 2: Update `meta.json`**
  - Remove the `llm-gguf` entry.
  - Change the `mlx-llm-chat` entry's `"languages"` from `["python","swift"]` to `["python"]` (the Swift MLX variant now lives inside `llm-chat`; `python/mlx-llm-chat` stays).
  - Add a new entry: `{ "name": "llm-chat", "description": "Swift LLM chat (Hummingbird + React) with a gguf (Jetson/llama.cpp) or mlx (Apple Silicon) backend", "languages": ["swift"] }`.

- [ ] **Step 3: Update `README.md`** — replace the `llm-gguf` / swift `mlx-llm-chat` sections with an `llm-chat` section documenting the `BACKEND` variable (`gguf` default = Jetson/llama.cpp; `mlx` = Apple Silicon only).

- [ ] **Step 4: Verify meta valid + in sync with dirs**

Run:
```bash
python3 -m json.tool meta.json >/dev/null && echo "meta ok"
grep -n "llm-gguf" meta.json README.md || echo "no stray llm-gguf refs"
ls -d swift/llm-chat swift/mlx-llm-chat swift/llm-gguf 2>&1
```
Expected: `meta ok`; no stray `llm-gguf`; only `swift/llm-chat` exists (the other two gone).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(swift): replace mlx-llm-chat + llm-gguf with unified llm-chat (BACKEND variable)"
```

---

## Final verification (after all phases)

- [ ] `python3 -m json.tool meta.json >/dev/null && echo ok` → `ok`.
- [ ] Every `meta.json` template `name` has matching on-disk dirs for each declared language; no directory exists without a meta entry. Spot-check: `camera-feed` gone everywhere; `webcam` present in all 5; `swift/llm-chat` present, `swift/{mlx-llm-chat,llm-gguf}` gone; `swift/bluetooth-discovery` present.
- [ ] `git grep -n "camera-feed[^-]\|llm-gguf" -- ':!docs/'` returns nothing (docs specs/plans may reference them historically).
- [ ] README language tables updated for `webcam`, `llm-chat`, `bluetooth-discovery`.
```
