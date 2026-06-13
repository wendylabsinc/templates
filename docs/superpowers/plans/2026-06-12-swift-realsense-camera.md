# Swift `realsense-camera` Template Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `swift/realsense-camera`, a Swift port of the RealSense D415 multi-stream MJPEG viewer template, using Swift C++ interop against librealsense.

**Architecture:** A `noexcept` C++ shim target (`RealSenseKit`) wraps all throwing librealsense calls (C++ exceptions must never reach Swift). The Swift app target (C++ interop enabled) runs the blocking capture loop on a dedicated `Thread`, encodes frames with TurboJPEG via C interop, publishes into a `FrameStore` actor, and serves the same HTTP contract the shared frontend already speaks (`/start`, `/stop`, `/config`, `/health`, `/stream/{id}` MJPEG).

**Tech Stack:** Swift 6.2 tools, Hummingbird 2, swift-otel, librealsense v2.55.1 (C++ interop), TurboJPEG, vendored React/Vite frontend from `common/realsense-camera-frontend/`.

**Spec:** `docs/superpowers/specs/2026-06-12-swift-realsense-camera-design.md`

**Testing note:** This repo ships templates, not apps — there is no unit-test infrastructure in any template. Verification is build-based: `swift build` on a rendered copy (template vars substituted) plus a full `docker build`. Live D415 streaming is a manual hardware step flagged at the end.

**Template variable note:** Files contain literal `{{.APP_ID}}`, `{{.PORT}}`, `{{.SWIFT_VERSION}}` tokens (Go-template style, substituted by `wendy init`). The Swift sources directory is named after the *template* (`Sources/realsense-camera/`) per repo convention — the CLI renames it to the app id at init time.

---

### Task 1: Scaffold template directory, config files, vendored frontend

**Files:**
- Create: `swift/realsense-camera/template.json`
- Create: `swift/realsense-camera/wendy.json`
- Create: vendored frontend files (copied from `common/realsense-camera-frontend/`)

- [ ] **Step 1: Create directory and copy the vendored frontend**

```bash
cd /Users/joannisorlandos/git/wendy/templates
mkdir -p swift/realsense-camera
cp -R common/realsense-camera-frontend/src swift/realsense-camera/src
cp -R common/realsense-camera-frontend/public swift/realsense-camera/public
cp common/realsense-camera-frontend/index.html \
   common/realsense-camera-frontend/package.json \
   common/realsense-camera-frontend/package-lock.json \
   common/realsense-camera-frontend/components.json \
   common/realsense-camera-frontend/eslint.config.js \
   common/realsense-camera-frontend/tsconfig.json \
   common/realsense-camera-frontend/tsconfig.app.json \
   common/realsense-camera-frontend/tsconfig.node.json \
   common/realsense-camera-frontend/vite.config.ts \
   swift/realsense-camera/
```

(Do NOT copy the frontend's `README.md` — the cpp/python templates don't vendor it either.)

- [ ] **Step 2: Verify the copy matches the cpp template's vendoring**

Run: `diff -rq common/realsense-camera-frontend/src cpp/realsense-camera/src && diff -rq common/realsense-camera-frontend/src swift/realsense-camera/src`
Expected: no output (identical trees).

- [ ] **Step 3: Write `swift/realsense-camera/template.json`**

```json
{
    "name": "realsense-camera",
    "description": "Live multi-stream viewer for Intel RealSense D415: Swift C++ interop with librealsense, color + 2x IR + depth as MJPEG",
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
            "description": "HTTP port (serves the SPA and the MJPEG endpoints)",
            "type": "integer",
            "default": 6007,
            "prompt": "HTTP port",
            "validate": { "min": 1, "max": 65535 }
        },
        {
            "name": "SWIFT_VERSION",
            "description": "Swift toolchain version",
            "type": "string",
            "default": "6.3",
            "prompt": "Swift version"
        }
    ]
}
```

- [ ] **Step 4: Write `swift/realsense-camera/wendy.json`** (same entitlements/readiness as the cpp template)

```json
{
    "appId": "{{.APP_ID}}",
    "version": "0.1.0",
    "entitlements": [
        { "type": "usb" },
        { "type": "network", "mode": "host" }
    ],
    "readiness": {
        "tcpSocket": { "port": {{.PORT}} },
        "timeoutSeconds": 60
    },
    "hooks": {
        "postStart": {
            "cli": "wendy utils open-browser http://${WENDY_HOSTNAME}:{{.PORT}}"
        }
    }
}
```

- [ ] **Step 5: Commit**

```bash
git add swift/realsense-camera
git commit -m "Scaffold swift/realsense-camera: config + vendored frontend"
```

---

### Task 2: Package manifest and system library targets

**Files:**
- Create: `swift/realsense-camera/Package.swift`
- Create: `swift/realsense-camera/Sources/CRealsense2/module.modulemap`
- Create: `swift/realsense-camera/Sources/CRealsense2/shim.h`
- Create: `swift/realsense-camera/Sources/CTurboJPEG/module.modulemap`
- Create: `swift/realsense-camera/Sources/CTurboJPEG/shim.h`

- [ ] **Step 1: Write `Package.swift`**

```swift
// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "{{.APP_ID}}",
    platforms: [.macOS("26.0")],
    dependencies: [
        .package(url: "https://github.com/hummingbird-project/hummingbird.git", from: "2.21.1", traits: []),
        .package(url: "https://github.com/apple/swift-container-plugin", from: "1.0.0"),
        .package(url: "https://github.com/swift-otel/swift-otel.git", from: "1.0.0", traits: ["OTLPHTTP", "OTLPGRPC"]),
    ],
    targets: [
        .systemLibrary(name: "CRealsense2", pkgConfig: "realsense2"),
        .systemLibrary(name: "CTurboJPEG", pkgConfig: "libturbojpeg"),
        .target(
            name: "RealSenseKit",
            dependencies: ["CRealsense2"]
        ),
        .executableTarget(
            name: "{{.APP_ID}}",
            dependencies: [
                "RealSenseKit",
                "CTurboJPEG",
                .product(name: "Hummingbird", package: "hummingbird"),
                .product(name: "OTel", package: "swift-otel"),
            ],
            swiftSettings: [
                .interoperabilityMode(.Cxx)
            ]
        ),
    ],
    cxxLanguageStandard: .cxx17
)
```

Notes for the implementer:
- `RealSenseKit` is a pure C++ target (only `.cpp`/`.hpp` files in its directory) — SwiftPM infers C++ from the sources; no `swiftSettings` there.
- The executable target needs `.interoperabilityMode(.Cxx)` to import `RealSenseKit`'s C++ header. Hummingbird/OTel are plain Swift dependencies and are unaffected.
- No `hummingbird-websocket` dependency (unlike `swift/camera-feed`): MJPEG here is plain HTTP multipart.

- [ ] **Step 2: Write `Sources/CRealsense2/module.modulemap`**

```
module CRealsense2 {
    header "shim.h"
    link "realsense2"
    export *
}
```

- [ ] **Step 3: Write `Sources/CRealsense2/shim.h`**

```c
#include <librealsense2/rs.h>
```

(The modulemap maps the C header; `RealSenseKit.cpp` includes `<librealsense2/rs.hpp>` directly — the include/link paths flow from pkg-config through this systemLibrary dependency.)

- [ ] **Step 4: Write `Sources/CTurboJPEG/module.modulemap`**

```
module CTurboJPEG {
    header "shim.h"
    link "turbojpeg"
    export *
}
```

- [ ] **Step 5: Write `Sources/CTurboJPEG/shim.h`**

```c
#include <turbojpeg.h>
```

- [ ] **Step 6: Commit**

```bash
git add swift/realsense-camera/Package.swift swift/realsense-camera/Sources
git commit -m "Add swift/realsense-camera package manifest and system library targets"
```

---

### Task 3: RealSenseKit C++ shim

**Files:**
- Create: `swift/realsense-camera/Sources/RealSenseKit/include/RealSenseKit.hpp`
- Create: `swift/realsense-camera/Sources/RealSenseKit/RealSenseKit.cpp`

- [ ] **Step 1: Write `include/RealSenseKit.hpp`**

```cpp
#pragma once

#include <cstdint>

// Thin noexcept shim over librealsense. Every function catches rs2::error /
// std::exception internally: C++ exceptions must never propagate into Swift
// (Swift cannot catch them; the process would abort).
namespace rsk {

enum class PixelFormat : int {
    bgr8 = 0,
    y8 = 1,
    rgb8 = 2,
};

// A borrowed view into librealsense-owned pixel memory. Valid only until the
// next cameraWaitForFrames / cameraStop / cameraDestroy call on the same
// camera. The capture loop encodes to JPEG before waiting again, so views
// never outlive their frameset.
struct FrameView {
    const uint8_t *data = nullptr;
    int width = 0;
    int height = 0;
    PixelFormat format = PixelFormat::bgr8;

    bool isValid() const { return data != nullptr; }
};

struct FrameBatch {
    bool ok = false;
    FrameView color;    // BGR8
    FrameView irLeft;   // Y8
    FrameView irRight;  // Y8
    FrameView depth;    // colorized RGB8
};

class Camera;

Camera *cameraCreate() noexcept;
void cameraDestroy(Camera *camera) noexcept;

// Starts color + depth + 2x IR at the given mode. Retries pipeline.start 3x
// with 500ms backoff. Returns false if the pipeline could not be started
// (no device, unsupported mode, USB error).
bool cameraStart(Camera *camera, int width, int height, int fps) noexcept;
void cameraStop(Camera *camera) noexcept;

// Applies a D400 visual preset by name ("default", "hand", "high-accuracy",
// "high-density", "medium-density"). Returns false for unknown names or when
// no sensor supports presets.
bool cameraApplyPreset(Camera *camera, const char *name) noexcept;

// Blocks up to timeoutMs for the next frameset. batch.ok == false on timeout
// or device error; the caller decides whether to keep polling.
FrameBatch cameraWaitForFrames(Camera *camera, int timeoutMs) noexcept;

} // namespace rsk
```

- [ ] **Step 2: Write `RealSenseKit.cpp`**

```cpp
#include "RealSenseKit.hpp"

#include <librealsense2/rs.hpp>

#include <chrono>
#include <cstdio>
#include <cstring>
#include <optional>
#include <thread>

namespace rsk {

class Camera {
  public:
    rs2::pipeline pipeline;
    std::optional<rs2::pipeline_profile> profile;
    std::optional<rs2::sensor> presetSensor;
    rs2::colorizer colorizer;
    // Keep the last frameset and colorized depth frame alive so the
    // FrameViews handed to Swift stay valid until the next wait call.
    rs2::frameset lastFrames;
    rs2::frame lastDepth;
    bool started = false;
};

namespace {

struct PresetEntry {
    const char *name;
    rs2_rs400_visual_preset value;
};

constexpr PresetEntry kPresets[] = {
    {"default", RS2_RS400_VISUAL_PRESET_DEFAULT},
    {"hand", RS2_RS400_VISUAL_PRESET_HAND},
    {"high-accuracy", RS2_RS400_VISUAL_PRESET_HIGH_ACCURACY},
    {"high-density", RS2_RS400_VISUAL_PRESET_HIGH_DENSITY},
    {"medium-density", RS2_RS400_VISUAL_PRESET_MEDIUM_DENSITY},
};

FrameView makeView(const rs2::video_frame &frame, PixelFormat format)
{
    FrameView view;
    view.data = static_cast<const uint8_t *>(frame.get_data());
    view.width = frame.get_width();
    view.height = frame.get_height();
    view.format = format;
    return view;
}

} // namespace

Camera *cameraCreate() noexcept
{
    try
    {
        return new Camera();
    }
    catch (...)
    {
        return nullptr;
    }
}

void cameraDestroy(Camera *camera) noexcept
{
    if (!camera)
    {
        return;
    }
    cameraStop(camera);
    delete camera;
}

bool cameraStart(Camera *camera, int width, int height, int fps) noexcept
{
    if (!camera)
    {
        return false;
    }
    if (camera->started)
    {
        return true;
    }

    try
    {
        rs2::config config;
        config.enable_stream(RS2_STREAM_COLOR, width, height, RS2_FORMAT_BGR8, fps);
        config.enable_stream(RS2_STREAM_DEPTH, width, height, RS2_FORMAT_Z16, fps);
        config.enable_stream(RS2_STREAM_INFRARED, 1, width, height, RS2_FORMAT_Y8, fps);
        config.enable_stream(RS2_STREAM_INFRARED, 2, width, height, RS2_FORMAT_Y8, fps);

        bool startedPipeline = false;
        for (int attempt = 1; attempt <= 3; ++attempt)
        {
            try
            {
                camera->profile.emplace(camera->pipeline.start(config));
                startedPipeline = true;
                break;
            }
            catch (const rs2::error &e)
            {
                std::fprintf(stderr, "[RealSenseKit] pipeline.start attempt %d/3 failed at %dx%d@%dfps: %s\n",
                             attempt, width, height, fps, e.what());
                if (attempt < 3)
                {
                    std::this_thread::sleep_for(std::chrono::milliseconds(500));
                }
            }
        }
        if (!startedPipeline)
        {
            return false;
        }

        camera->presetSensor.reset();
        try
        {
            for (auto &&sensor : camera->profile->get_device().query_sensors())
            {
                if (sensor.supports(RS2_OPTION_VISUAL_PRESET))
                {
                    camera->presetSensor.emplace(sensor);
                    break;
                }
            }
        }
        catch (const rs2::error &e)
        {
            std::fprintf(stderr, "[RealSenseKit] could not inspect sensors for presets: %s\n", e.what());
        }

        camera->started = true;
        return true;
    }
    catch (const std::exception &e)
    {
        std::fprintf(stderr, "[RealSenseKit] start failed: %s\n", e.what());
        return false;
    }
    catch (...)
    {
        return false;
    }
}

void cameraStop(Camera *camera) noexcept
{
    if (!camera)
    {
        return;
    }
    if (camera->started)
    {
        try
        {
            camera->pipeline.stop();
        }
        catch (...)
        {
        }
    }
    camera->lastFrames = rs2::frameset();
    camera->lastDepth = rs2::frame();
    camera->profile.reset();
    camera->presetSensor.reset();
    camera->started = false;
}

bool cameraApplyPreset(Camera *camera, const char *name) noexcept
{
    if (!camera || !name || !camera->presetSensor)
    {
        return false;
    }

    const PresetEntry *entry = nullptr;
    for (const auto &candidate : kPresets)
    {
        if (std::strcmp(candidate.name, name) == 0)
        {
            entry = &candidate;
            break;
        }
    }
    if (!entry)
    {
        std::fprintf(stderr, "[RealSenseKit] unknown visual preset: %s\n", name);
        return false;
    }

    try
    {
        camera->presetSensor->set_option(RS2_OPTION_VISUAL_PRESET, static_cast<float>(entry->value));
        return true;
    }
    catch (const rs2::error &e)
    {
        std::fprintf(stderr, "[RealSenseKit] failed to apply preset %s: %s\n", name, e.what());
        return false;
    }
    catch (...)
    {
        return false;
    }
}

FrameBatch cameraWaitForFrames(Camera *camera, int timeoutMs) noexcept
{
    FrameBatch batch;
    if (!camera || !camera->started)
    {
        return batch;
    }

    try
    {
        rs2::frameset frames;
        if (!camera->pipeline.try_wait_for_frames(&frames, static_cast<unsigned int>(timeoutMs)))
        {
            return batch;
        }
        camera->lastFrames = frames;

        if (auto color = frames.get_color_frame())
        {
            batch.color = makeView(color, PixelFormat::bgr8);
        }
        if (auto irLeft = frames.get_infrared_frame(1))
        {
            batch.irLeft = makeView(irLeft, PixelFormat::y8);
        }
        if (auto irRight = frames.get_infrared_frame(2))
        {
            batch.irRight = makeView(irRight, PixelFormat::y8);
        }
        if (auto depth = frames.get_depth_frame())
        {
            camera->lastDepth = camera->colorizer.colorize(depth);
            batch.depth = makeView(camera->lastDepth.as<rs2::video_frame>(), PixelFormat::rgb8);
        }

        batch.ok = true;
        return batch;
    }
    catch (const std::exception &e)
    {
        std::fprintf(stderr, "[RealSenseKit] wait_for_frames error: %s\n", e.what());
        return FrameBatch{};
    }
    catch (...)
    {
        return FrameBatch{};
    }
}

} // namespace rsk
```

- [ ] **Step 3: Commit**

```bash
git add swift/realsense-camera/Sources/RealSenseKit
git commit -m "Add RealSenseKit noexcept C++ shim over librealsense"
```

---

### Task 4: Swift JPEG encoder

**Files:**
- Create: `swift/realsense-camera/Sources/realsense-camera/JPEGEncoder.swift`

- [ ] **Step 1: Write `JPEGEncoder.swift`**

```swift
import CTurboJPEG
import RealSenseKit

// TJFLAG_FASTDCT is a #define and doesn't import into Swift.
private let tjFlagFastDCT: Int32 = 2048

enum JPEGEncoder {
    static let quality: Int32 = 80

    // Encodes one frame view with TurboJPEG. Must run on the capture thread,
    // while the view's backing frameset is still alive (i.e. before the next
    // cameraWaitForFrames call). Returns nil on encode failure.
    static func encode(_ view: rsk.FrameView, encoder: OpaquePointer?) -> [UInt8]? {
        guard view.isValid(), let encoder else { return nil }

        let pixelFormat: Int32
        let subsampling: Int32
        switch view.format {
        case .bgr8:
            pixelFormat = Int32(TJPF_BGR.rawValue)
            subsampling = Int32(TJSAMP_420.rawValue)
        case .y8:
            pixelFormat = Int32(TJPF_GRAY.rawValue)
            subsampling = Int32(TJSAMP_GRAY.rawValue)
        case .rgb8:
            pixelFormat = Int32(TJPF_RGB.rawValue)
            subsampling = Int32(TJSAMP_420.rawValue)
        }

        var jpegBuffer: UnsafeMutablePointer<UInt8>? = nil
        var jpegSize: UInt = 0
        let rc = tjCompress2(
            encoder, view.data, Int32(view.width), 0, Int32(view.height),
            pixelFormat, &jpegBuffer, &jpegSize, subsampling, quality, tjFlagFastDCT
        )
        guard rc == 0, let buffer = jpegBuffer else {
            if let buffer = jpegBuffer { tjFree(buffer) }
            return nil
        }
        defer { tjFree(buffer) }
        return Array(UnsafeBufferPointer(start: buffer, count: Int(jpegSize)))
    }
}
```

Implementer notes (likely compile adjustments, fix in place if the importer differs):
- `TJPF_BGR`/`TJSAMP_420` are plain C enums; if they import as `Int32` directly, drop the `.rawValue`.
- `tjCompress2`'s size parameter is C `unsigned long` → Swift `UInt` on Linux/macOS 64-bit. If the importer wants `CUnsignedLong`, adjust the `jpegSize` type.
- `rsk.PixelFormat` is a C++ `enum class` and imports as a Swift enum; the `switch` must stay exhaustive without `default`.

- [ ] **Step 2: Commit**

```bash
git add swift/realsense-camera/Sources/realsense-camera/JPEGEncoder.swift
git commit -m "Add TurboJPEG encoder wrapper for RealSense frame views"
```

---

### Task 5: FrameStore actor

**Files:**
- Create: `swift/realsense-camera/Sources/realsense-camera/FrameStore.swift`

- [ ] **Step 1: Write `FrameStore.swift`**

```swift
internal import Foundation

struct FrameSnapshot: Sendable {
    let jpeg: [UInt8]
    let sequence: UInt64
}

struct HealthStatus: Encodable {
    let streams: [String]
    let running: Bool
    let fps: [String: Double]
}

// Holds the latest encoded frame per stream and wakes waiting MJPEG handlers
// on publish. Mirrors the C++ template's RealSensePump frame/fps bookkeeping.
actor FrameStore {
    static let streamIds = ["color", "ir-left", "ir-right", "depth"]

    private var latest: [String: FrameSnapshot] = [:]
    private(set) var running = false
    private var waiters: [UUID: CheckedContinuation<Void, Never>] = [:]

    private var fpsCounts: [String: Int] = [:]
    private var fpsLatest: [String: Double] = [:]
    private var fpsWindowStart = ContinuousClock.now

    func setRunning(_ value: Bool) {
        running = value
        if !value {
            resetFPS()
        }
        notifyAll()
    }

    func clear() {
        latest.removeAll()
        resetFPS()
        notifyAll()
    }

    func publish(_ updates: [String: [UInt8]]) {
        guard !updates.isEmpty else { return }
        for (stream, jpeg) in updates {
            let sequence = (latest[stream]?.sequence ?? 0) + 1
            latest[stream] = FrameSnapshot(jpeg: jpeg, sequence: sequence)
            fpsCounts[stream, default: 0] += 1
        }

        let elapsed = fpsWindowStart.duration(to: .now)
        if elapsed >= .seconds(1) {
            let seconds = Double(elapsed.components.seconds)
                + Double(elapsed.components.attoseconds) / 1e18
            for stream in Self.streamIds {
                fpsLatest[stream] = (Double(fpsCounts[stream] ?? 0) / seconds * 10).rounded() / 10
                fpsCounts[stream] = 0
            }
            fpsWindowStart = .now
        }
        notifyAll()
    }

    func health() -> HealthStatus {
        HealthStatus(
            streams: Self.streamIds,
            running: running,
            fps: Dictionary(uniqueKeysWithValues: Self.streamIds.map { ($0, fpsLatest[$0] ?? 0) })
        )
    }

    // Returns the next frame for `stream` newer than `sequence`. Returns nil
    // when the pump is stopped or when `timeout` passes with no new frame —
    // the MJPEG handler then ends its response (same semantics as the C++
    // template's waitForFrame).
    func waitForFrame(stream: String, after sequence: UInt64, timeout: Duration) async -> FrameSnapshot? {
        let deadline = ContinuousClock.now + timeout
        while true {
            if let snapshot = latest[stream], snapshot.sequence != sequence {
                return snapshot
            }
            if !running {
                return nil
            }
            let remaining = ContinuousClock.now.duration(to: deadline)
            if remaining <= .zero {
                return nil
            }
            await waitForChange(upTo: remaining)
        }
    }

    private func waitForChange(upTo duration: Duration) async {
        let id = UUID()
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            waiters[id] = continuation
            Task {
                try? await Task.sleep(for: duration)
                self.expire(id)
            }
        }
    }

    private func expire(_ id: UUID) {
        waiters.removeValue(forKey: id)?.resume()
    }

    private func notifyAll() {
        let pending = waiters
        waiters.removeAll()
        for (_, continuation) in pending {
            continuation.resume()
        }
    }

    private func resetFPS() {
        fpsCounts.removeAll()
        for stream in Self.streamIds {
            fpsLatest[stream] = 0
        }
        fpsWindowStart = .now
    }
}
```

Implementer note: the `Task { ... self.expire(id) }` inside the actor runs on the actor, so `self.expire(id)` needs no `await` only if the compiler infers actor isolation for the closure; if it complains, write `await self.expire(id)`.

- [ ] **Step 2: Commit**

```bash
git add swift/realsense-camera/Sources/realsense-camera/FrameStore.swift
git commit -m "Add FrameStore actor: latest-frame fan-out with fps tracking"
```

---

### Task 6: RealSensePump (capture thread + lifecycle)

**Files:**
- Create: `swift/realsense-camera/Sources/realsense-camera/RealSensePump.swift`

- [ ] **Step 1: Write `RealSensePump.swift`**

```swift
internal import Foundation
import CTurboJPEG
import Logging
import RealSenseKit
import Synchronization

struct PumpConfig: Sendable, Equatable {
    var width = 640
    var height = 480
    var fps = 30
    var preset = "default"

    static let presets = ["default", "hand", "high-accuracy", "high-density", "medium-density"]
}

// State shared between the pump actor and the capture thread. The thread
// polls these between frames; frame data flows through an AsyncStream so
// publish order is preserved.
private final class WorkerShared: Sendable {
    let stopRequested = Atomic<Bool>(false)
    let pendingPreset = Mutex<String?>(nil)
}

// Owns the capture worker lifecycle: a dedicated Thread runs the blocking
// librealsense loop (never the cooperative pool), encodes frames with
// TurboJPEG, and yields batches into an AsyncStream consumed by a Task that
// publishes to the FrameStore in order.
actor RealSensePump {
    private let store: FrameStore
    private let logger: Logger
    private var config = PumpConfig()
    private var worker: (shared: WorkerShared, consumer: Task<Void, Never>)?

    init(store: FrameStore, logger: Logger) {
        self.store = store
        self.logger = logger
    }

    func start() async {
        guard worker == nil else { return }
        await store.setRunning(true)
        spawnWorker()
    }

    func stop() async {
        guard let worker else {
            await store.setRunning(false)
            return
        }
        self.worker = nil
        worker.shared.stopRequested.store(true, ordering: .sequentiallyConsistent)
        await store.setRunning(false)
        await worker.consumer.value
        await store.clear()
    }

    func configure(width: Int, height: Int, fps: Int, preset: String) async {
        let modeChanged = width != config.width || height != config.height || fps != config.fps
        config = PumpConfig(width: width, height: height, fps: fps, preset: preset)

        guard let worker else { return }
        if modeChanged {
            // Restart the pipeline at the new mode, keeping `running` true so
            // connected MJPEG clients ride through the gap (C++ parity).
            self.worker = nil
            worker.shared.stopRequested.store(true, ordering: .sequentiallyConsistent)
            await worker.consumer.value
            spawnWorker()
        } else {
            worker.shared.pendingPreset.withLock { $0 = preset }
        }
    }

    private func spawnWorker() {
        let shared = WorkerShared()
        shared.pendingPreset.withLock { $0 = config.preset }

        let (frames, continuation) = AsyncStream.makeStream(
            of: [String: [UInt8]].self,
            bufferingPolicy: .bufferingNewest(4)
        )

        let config = self.config
        let logger = self.logger
        let thread = Thread {
            Self.captureLoop(shared: shared, config: config, logger: logger, continuation: continuation)
        }
        thread.name = "realsense-pump"
        thread.start()

        let store = self.store
        let consumer = Task {
            for await updates in frames {
                await store.publish(updates)
            }
            // The capture thread exited. If this wasn't an explicit stop or
            // restart, the pipeline died (no device, USB error) — reflect it.
            if !shared.stopRequested.load(ordering: .sequentiallyConsistent) {
                await store.setRunning(false)
                await self.reap(shared)
            }
        }
        worker = (shared, consumer)
    }

    private func reap(_ shared: WorkerShared) {
        if worker?.shared === shared {
            worker = nil
        }
    }

    private static func captureLoop(
        shared: WorkerShared,
        config: PumpConfig,
        logger: Logger,
        continuation: AsyncStream<[String: [UInt8]]>.Continuation
    ) {
        defer { continuation.finish() }

        guard let camera = rsk.cameraCreate() else {
            logger.error("Failed to create RealSense camera")
            return
        }
        defer { rsk.cameraDestroy(camera) }

        guard rsk.cameraStart(camera, Int32(config.width), Int32(config.height), Int32(config.fps)) else {
            logger.error("Failed to start RealSense pipeline at \(config.width)x\(config.height)@\(config.fps)fps")
            return
        }
        defer { rsk.cameraStop(camera) }

        guard let encoder = tjInitCompress() else {
            logger.error("Failed to initialize TurboJPEG encoder")
            return
        }
        defer { tjDestroy(encoder) }

        logger.info("RealSense pipeline started at \(config.width)x\(config.height)@\(config.fps)fps")

        while !shared.stopRequested.load(ordering: .sequentiallyConsistent) {
            let pendingPreset = shared.pendingPreset.withLock { (value: inout String?) -> String? in
                let preset = value
                value = nil
                return preset
            }
            if let preset = pendingPreset {
                let applied = preset.withCString { rsk.cameraApplyPreset(camera, $0) }
                if applied {
                    logger.info("Applied RealSense visual preset: \(preset)")
                }
            }

            let batch = rsk.cameraWaitForFrames(camera, 1000)
            guard batch.ok else { continue }

            var updates: [String: [UInt8]] = [:]
            if let jpeg = JPEGEncoder.encode(batch.color, encoder: encoder) { updates["color"] = jpeg }
            if let jpeg = JPEGEncoder.encode(batch.irLeft, encoder: encoder) { updates["ir-left"] = jpeg }
            if let jpeg = JPEGEncoder.encode(batch.irRight, encoder: encoder) { updates["ir-right"] = jpeg }
            if let jpeg = JPEGEncoder.encode(batch.depth, encoder: encoder) { updates["depth"] = jpeg }
            continuation.yield(updates)
        }
        logger.info("RealSense pipeline stopped")
    }
}
```

Implementer notes:
- `Atomic`/`Mutex` come from the `Synchronization` module (Swift 6 stdlib, available on Linux and macOS 15+ — the manifest's `.macOS("26.0")` covers it).
- `rsk.cameraCreate()` returns an `OpaquePointer?` in Swift (the C++ `Camera` class is forward-declared only).
- If `Thread` is unavailable without it, `internal import Foundation` already covers it; on Linux this works in the swift docker images.

- [ ] **Step 2: Commit**

```bash
git add swift/realsense-camera/Sources/realsense-camera/RealSensePump.swift
git commit -m "Add RealSensePump: dedicated capture thread with actor lifecycle"
```

---

### Task 7: HTTP app (routes, MJPEG streaming, static SPA)

**Files:**
- Create: `swift/realsense-camera/Sources/realsense-camera/StaticFileMiddleware.swift` (copy from `swift/camera-feed/Sources/camera-feed/StaticFileMiddleware.swift`, unchanged)
- Create: `swift/realsense-camera/Sources/realsense-camera/App.swift`

- [ ] **Step 1: Copy `StaticFileMiddleware.swift` verbatim**

```bash
cp swift/camera-feed/Sources/camera-feed/StaticFileMiddleware.swift \
   swift/realsense-camera/Sources/realsense-camera/StaticFileMiddleware.swift
```

- [ ] **Step 2: Write `App.swift`**

```swift
internal import Foundation
import Hummingbird
import Logging
import NIOCore
import OTel
import ServiceLifecycle

struct RunningStatus: Encodable {
    let running: Bool
}

struct ConfigResponse: Encodable {
    let width: Int
    let height: Int
    let fps: Int
    let preset: String
}

struct ErrorResponse: Encodable {
    let error: String
}

private enum ParamError: Error {
    case invalid(String)
}

private func jsonResponse(_ value: some Encodable, status: HTTPResponse.Status = .ok) throws -> Response {
    let data = try JSONEncoder().encode(value)
    var buffer = ByteBuffer()
    buffer.writeBytes(data)
    return Response(
        status: status,
        headers: [.contentType: "application/json"],
        body: .init(byteBuffer: buffer)
    )
}

private func intParam(_ request: Request, _ name: String, fallback: Int, min: Int, max: Int) throws -> Int {
    guard let raw = request.uri.queryParameters.get(name).map(String.init), !raw.isEmpty else {
        return fallback
    }
    guard let value = Int(raw) else {
        throw ParamError.invalid("\(name) must be an integer")
    }
    guard (min...max).contains(value) else {
        throw ParamError.invalid("\(name) must be between \(min) and \(max)")
    }
    return value
}

@main
struct App {
    static func main() async throws {
        let observability = try OTel.bootstrap()
        let logger = Logger(label: "{{.APP_ID}}")

        let store = FrameStore()
        let pump = RealSensePump(store: store, logger: logger)

        let router = Router()
        router.middlewares.add(TracingMiddleware())
        router.middlewares.add(MetricsMiddleware())

        router.post("/start") { _, _ -> Response in
            await pump.start()
            return try jsonResponse(RunningStatus(running: await store.running))
        }

        router.post("/stop") { _, _ -> Response in
            await pump.stop()
            return try jsonResponse(RunningStatus(running: await store.running))
        }

        router.post("/config") { request, _ -> Response in
            do {
                let width = try intParam(request, "width", fallback: 640, min: 1, max: 8192)
                let height = try intParam(request, "height", fallback: 480, min: 1, max: 8192)
                let fps = try intParam(request, "fps", fallback: 30, min: 1, max: 300)
                let preset = request.uri.queryParameters.get("preset").map(String.init) ?? "default"
                guard PumpConfig.presets.contains(preset) else {
                    return try jsonResponse(ErrorResponse(error: "Unknown preset: \(preset)"), status: .badRequest)
                }
                await pump.configure(width: width, height: height, fps: fps, preset: preset)
                return try jsonResponse(ConfigResponse(width: width, height: height, fps: fps, preset: preset))
            } catch ParamError.invalid(let message) {
                return try jsonResponse(ErrorResponse(error: message), status: .badRequest)
            }
        }

        router.get("/health") { _, _ -> Response in
            try jsonResponse(await store.health())
        }

        router.get("/stream/{streamId}") { _, context -> Response in
            let streamId = try context.parameters.require("streamId")
            guard FrameStore.streamIds.contains(streamId) else {
                return try jsonResponse(ErrorResponse(error: "Unknown stream: \(streamId)"), status: .notFound)
            }

            var headers = HTTPFields()
            headers[.contentType] = "multipart/x-mixed-replace; boundary=frame"
            headers[.cacheControl] = "no-store"
            return Response(status: .ok, headers: headers, body: .init { writer in
                var lastSequence: UInt64 = 0
                while let frame = await store.waitForFrame(
                    stream: streamId, after: lastSequence, timeout: .seconds(5)
                ) {
                    lastSequence = frame.sequence
                    var part = ByteBuffer()
                    part.writeString("--frame\r\nContent-Type: image/jpeg\r\nContent-Length: \(frame.jpeg.count)\r\n\r\n")
                    part.writeBytes(frame.jpeg)
                    part.writeString("\r\n")
                    try await writer.write(part)
                }
                try await writer.finish(nil)
            })
        }

        router.get("/", use: spaHandler(staticDir: "static"))
        router.get("{path+}", use: spaHandler(staticDir: "static"))

        let app = Application(
            router: router,
            configuration: .init(address: .hostname("0.0.0.0", port: {{.PORT}}))
        )

        let hostname = ProcessInfo.processInfo.environment["WENDY_HOSTNAME"] ?? "0.0.0.0"
        logger.info("Starting server on http://\(hostname):{{.PORT}}")

        try await ServiceGroup(
            services: [observability, app],
            gracefulShutdownSignals: [.sigterm, .sigint],
            logger: logger
        ).run()
    }
}
```

Implementer notes:
- `context.parameters.require(_:)` may return `String` or need `String(...)` around it depending on Hummingbird's parameter type; adjust to compile.
- The MJPEG body uses `ResponseBody.init(write:)` (closure receiving a `ResponseBodyWriter`); if the initializer label differs in this Hummingbird version, check `swift/camera-feed`'s resolved Hummingbird source under `.build/checkouts` for the exact signature.
- The client-disconnect path: `writer.write` throws when the client goes away, which exits the closure — that's the intended cleanup.

- [ ] **Step 3: Commit**

```bash
git add swift/realsense-camera/Sources/realsense-camera
git commit -m "Add HTTP app: start/stop/config/health, MJPEG streams, SPA"
```

---

### Task 8: Resolve dependencies + local build validation

**Files:**
- Create: `swift/realsense-camera/Package.resolved` (generated)

- [ ] **Step 1: Ensure native deps are installed (macOS dev machine)**

```bash
brew list librealsense &>/dev/null || brew install librealsense
brew list jpeg-turbo &>/dev/null || brew install jpeg-turbo
pkg-config --modversion realsense2 libturbojpeg
```

Expected: two version numbers print (e.g. `2.55.x` and `2.1.x`). If `pkg-config` can't find them, export `PKG_CONFIG_PATH="$(brew --prefix)/lib/pkgconfig:$(brew --prefix jpeg-turbo)/lib/pkgconfig"`.

- [ ] **Step 2: Render the template into a temp dir and build**

The template can't build in place ({{ tokens }} aren't valid Swift). Render a copy:

```bash
TMP=$(mktemp -d)
cp -R swift/realsense-camera "$TMP/app"
mv "$TMP/app/Sources/realsense-camera" "$TMP/app/Sources/rs-demo"
LC_ALL=C find "$TMP/app" \( -name '*.swift' -o -name '*.json' -o -name '*.ts' \) -type f \
  -exec sed -i '' -e 's/{{\.APP_ID}}/rs-demo/g' -e 's/{{\.PORT}}/6007/g' -e 's/{{\.SWIFT_VERSION}}/6.3/g' {} +
(cd "$TMP/app" && swift build 2>&1 | tail -20)
echo "$TMP"
```

Expected: `Build complete!`. Iterate on compile errors here (C++ interop importer quirks land in this step) and port every fix back to the template files (re-inserting `{{.APP_ID}}` / `{{.PORT}}` tokens where applicable).

- [ ] **Step 3: Copy the generated `Package.resolved` back into the template**

```bash
cp "$TMP/app/Package.resolved" swift/realsense-camera/Package.resolved
```

- [ ] **Step 4: Commit**

```bash
git add swift/realsense-camera
git commit -m "Add Package.resolved; fix compile issues found by local build"
```

---

### Task 9: Dockerfile

**Files:**
- Create: `swift/realsense-camera/Dockerfile`

- [ ] **Step 1: Write `Dockerfile`**

```dockerfile
# Stage 1: Build the React/Vite frontend.
FROM node:22-alpine AS frontend-builder
WORKDIR /build
COPY package.json package-lock.json ./
# Keep this aligned with the other RealSense templates. The upstream lockfile
# can drift on optional platform packages, so install refreshes it at build time.
RUN npm install --no-audit --no-fund
COPY . ./
RUN npm run build


# Stage 2: Build librealsense and the Swift backend.
FROM swift:{{.SWIFT_VERSION}}-bookworm AS backend-builder
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      build-essential cmake git pkg-config \
      libusb-1.0-0-dev libssl-dev libudev-dev \
      libturbojpeg0-dev \
    && rm -rf /var/lib/apt/lists/*

ARG LIBREALSENSE_VERSION=v2.55.1
RUN git clone --depth 1 --branch ${LIBREALSENSE_VERSION} \
      https://github.com/IntelRealSense/librealsense.git /tmp/librealsense \
    && cmake -S /tmp/librealsense -B /tmp/librealsense/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DBUILD_EXAMPLES=OFF \
        -DBUILD_GRAPHICAL_EXAMPLES=OFF \
        -DBUILD_TOOLS=OFF \
        -DBUILD_PYTHON_BINDINGS=OFF \
        -DFORCE_RSUSB_BACKEND=ON \
    && cmake --build /tmp/librealsense/build -j"$(nproc)" \
    && cmake --install /tmp/librealsense/build \
    && rm -rf /tmp/librealsense

ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig

WORKDIR /app
COPY Package.swift Package.resolved ./
COPY Sources Sources
RUN swift build -c release


# Stage 3: Runtime.
FROM swift:{{.SWIFT_VERSION}}-bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      libusb-1.0-0 libturbojpeg0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=backend-builder /usr/local/lib/librealsense2.so* /usr/local/lib/
RUN ldconfig

WORKDIR /app
COPY --from=backend-builder /app/.build/release/{{.APP_ID}} /usr/local/bin/{{.APP_ID}}
COPY --from=frontend-builder /build/dist /app/static

ARG WENDY_DEVICE_TYPE
ARG WENDY_DEBUG=false
ENV WENDY_DEVICE_TYPE=${WENDY_DEVICE_TYPE}
ENV WENDY_DEBUG=${WENDY_DEBUG}

EXPOSE {{.PORT}}

CMD ["{{.APP_ID}}"]
```

Note: `swift build` in stage 2 builds the directory `Sources/{{.APP_ID}}` — at this point `wendy init` has already renamed `Sources/realsense-camera` to the app id, same as every other Swift template.

- [ ] **Step 2: Commit**

```bash
git add swift/realsense-camera/Dockerfile
git commit -m "Add multi-stage Dockerfile for swift/realsense-camera"
```

---

### Task 10: Register in meta.json and README

**Files:**
- Modify: `meta.json` (realsense-camera entry: add `"swift"` to `languages`)
- Modify: `README.md` (realsense-camera section: add Swift row, update vendoring sentence)

- [ ] **Step 1: Edit `meta.json`** — change the realsense-camera entry to:

```json
  {
   "name": "realsense-camera",
   "description": "Live RealSense D415 multi-stream viewer: color + 2x IR + depth as MJPEG",
   "languages": [
    "python",
    "cpp",
    "swift"
   ]
  }
```

- [ ] **Step 2: Edit `README.md`** — in the `### realsense-camera` section, add the Swift row to the table and update the vendoring sentence:

```markdown
| Language | Framework | Default Port | Directory |
|----------|-----------|-------------|-----------|
| Python | FastAPI + pyrealsense2 | 8000 | `python/realsense-camera/` |
| C++ | Drogon + librealsense | 7007 | `cpp/realsense-camera/` |
| Swift | Hummingbird + librealsense (Swift C++ interop) | 6007 | `swift/realsense-camera/` |

The shared viewer frontend source lives at `common/realsense-camera-frontend/` and is vendored into each language template directory.
```

- [ ] **Step 3: Commit**

```bash
git add meta.json README.md
git commit -m "Register swift/realsense-camera in meta.json and README"
```

---

### Task 11: Full Docker build validation

- [ ] **Step 1: Render the template and build the image** (librealsense compiles from source; expect ~15–30 min)

```bash
TMP=$(mktemp -d)
cp -R swift/realsense-camera "$TMP/app"
mv "$TMP/app/Sources/realsense-camera" "$TMP/app/Sources/rs-demo"
LC_ALL=C find "$TMP/app" -type f \( -name '*.swift' -o -name '*.json' -o -name '*.ts' -o -name Dockerfile \) \
  -exec sed -i '' -e 's/{{\.APP_ID}}/rs-demo/g' -e 's/{{\.PORT}}/6007/g' -e 's/{{\.SWIFT_VERSION}}/6.3/g' {} +
docker build -t rs-demo-swift "$TMP/app" 2>&1 | tail -5
```

Expected: image builds successfully. Fix any Linux-only compile issues and port fixes back to the template (with tokens restored), then re-commit.

- [ ] **Step 2: Smoke-test the container without hardware**

```bash
docker run --rm -d -p 6007:6007 --name rs-demo-swift rs-demo-swift
sleep 3
curl -s http://localhost:6007/health
curl -s -X POST http://localhost:6007/start
sleep 2
curl -s http://localhost:6007/health
docker stop rs-demo-swift
```

Expected: `/health` returns `{"streams":["color","ir-left","ir-right","depth"],"running":false,...}`; after `/start` with no device attached, `running` flips back to `false` once the pipeline fails to start (3 retries ≈ 1.5 s) — no crash, server stays up.

- [ ] **Step 3: Final commit of any fixes**

```bash
git add swift/realsense-camera
git commit -m "Fix Linux build issues in swift/realsense-camera"
```

(Skip if nothing changed.)

---

### Manual hardware verification (flag at handoff, not automatable here)

On a WendyOS device with a D415 attached: `wendy init --app-id rs-test --template realsense-camera --language swift && cd rs-test && wendy run`, then verify all four streams render in the browser, `/config` resolution/preset changes apply, and `/health` fps values are non-zero.
