# whisper-stt (Swift) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a headless Swift `whisper-stt` template: capture from a USB mic, transcribe continuously with whisper.cpp (CUDA on Jetson), print + append each transcription to a persistent transcript file.

**Architecture:** One template directory `swift/whisper-stt/`. A single executable target captures 16 kHz mono `s16le` audio via `gstreamer-swift` (same path as `swift/audio`), buffers it into fixed-length chunks, gates silence by RMS, and transcribes each surviving chunk in-process by calling `libwhisper` through a local `CWhisper` C-interop target. whisper.cpp is built with `-DGGML_CUDA=ON` in a multi-stage Docker build on the same Jetson base `swift/llm-gguf` uses; the GGML model is baked into the image at build time.

**Tech Stack:** Swift 6.2, `gstreamer-swift` (branch `main`), whisper.cpp (pinned tag, `GGML_CUDA`), Docker multi-stage (`dustynv/tensorrt:8.6-r36.2.0` builder + runtime), Wendy templates conventions.

## Global Constraints

- Swift toolchain: **6.2** (`.swift-version` = `6.2`, `// swift-tools-version: 6.2`), matching `swift/audio`. The Dockerfile installs Swift 6.2.3 via the swift.org aarch64 tarball, exactly as `swift/llm-gguf` does.
- Audio dep: `.package(url: "https://github.com/wendylabsinc/gstreamer-swift.git", branch: "main")`, product `.product(name: "GStreamer", package: "gstreamer-swift")` — identical to `swift/audio`.
- Inference: **in-process `libwhisper`** via a local `.systemLibrary(name: "CWhisper")` target; **no** shell-out to `whisper-cli`.
- whisper.cpp is obtained by a **pinned `git clone --branch <tag>`** in the Dockerfile — **never** a vendored tarball (the explicit lesson from `llm-gguf`'s untracked `llama.cpp.tar.gz`).
- Base image: `dustynv/tensorrt:8.6-r36.2.0` (builder and runtime), matching `swift/llm-gguf`. `ARG CUDA_ARCH=87` (Orin).
- Template tokens: `{{.APP_ID}}`, `{{.WHISPER_MODEL}}` (substituted into all text files exactly as `{{.PORT}}` is in sibling templates). **No** `Package.resolved` is ever COPYed (repo `.gitignore` ignores it repo-wide).
- Config is environment-driven; no argparse-style CLI. Continuous mode only.
- Persistent transcript via a `persist` entitlement mounting `/data` (schema copied verbatim from `swift/persistent-volume`).
- New template must be registered in root `meta.json` and documented in root `README.md`.

---

### Task 1: Package + CWhisper C-interop target

**Files:**
- Create: `swift/whisper-stt/.swift-version`
- Create: `swift/whisper-stt/Package.swift`
- Create: `swift/whisper-stt/Sources/CWhisper/module.modulemap`
- Create: `swift/whisper-stt/Sources/CWhisper/shim.h`

**Interfaces:**
- Produces: a SwiftPM package named `{{.APP_ID}}` with an executable target `{{.APP_ID}}` depending on `GStreamer` and the local `CWhisper` module. `CWhisper` re-exports the whisper.cpp C API (`whisper_init_from_file_with_params`, `whisper_full`, `whisper_full_n_segments`, `whisper_full_get_segment_text`, `whisper_free`, `whisper_context_default_params`, `whisper_full_default_params`, `WHISPER_SAMPLING_GREEDY`) to Task 2's `main.swift`.

- [ ] **Step 1: Write `.swift-version`**

File `swift/whisper-stt/.swift-version`:
```
6.2
```

- [ ] **Step 2: Write `Package.swift`**

File `swift/whisper-stt/Package.swift`:
```swift
// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "{{.APP_ID}}",
    platforms: [
        .macOS("26.0"),
    ],
    dependencies: [
        .package(url: "https://github.com/wendylabsinc/gstreamer-swift.git", branch: "main"),
    ],
    targets: [
        .systemLibrary(
            name: "CWhisper",
            path: "Sources/CWhisper"
        ),
        .executableTarget(
            name: "{{.APP_ID}}",
            dependencies: [
                .product(name: "GStreamer", package: "gstreamer-swift"),
                "CWhisper",
            ]
        ),
    ]
)
```

- [ ] **Step 3: Write the C module map**

File `swift/whisper-stt/Sources/CWhisper/module.modulemap`:
```
module CWhisper {
    header "shim.h"
    link "whisper"
    export *
}
```

- [ ] **Step 4: Write the C shim header**

File `swift/whisper-stt/Sources/CWhisper/shim.h`:
```c
#pragma once
#include <whisper.h>
```
(whisper.h is installed to `/usr/local/include` by the Dockerfile's `cmake --install`; the `swift build` in Task 3 passes `-Xcc -I/usr/local/include` so the angle-bracket include resolves. whisper.h pulls in `ggml.h` from the same prefix.)

- [ ] **Step 5: Verify the tree**

Run: `find swift/whisper-stt -type f | sort`
Expected: the four files above.

- [ ] **Step 6: Commit**

```bash
git add swift/whisper-stt/.swift-version swift/whisper-stt/Package.swift swift/whisper-stt/Sources/CWhisper
git commit -m "feat: add package + CWhisper C-interop target for swift whisper-stt"
```

---

### Task 2: Transcriber node (`main.swift`)

**Files:**
- Create: `swift/whisper-stt/Sources/whisper-stt/main.swift`

> NOTE: the source directory is literally `Sources/whisper-stt` in the tracked
> template. The rendered target name is `{{.APP_ID}}`, so at render time the
> directory is renamed to `Sources/<app-id>` (the same render step the
> validation task performs); the tracked name is `whisper-stt` to keep the repo
> readable. `swift build` maps the `.executableTarget(name: "{{.APP_ID}}")` to
> `Sources/<app-id>` after substitution.

**Interfaces:**
- Consumes: `CWhisper` (from Task 1) and `GStreamer`'s `AudioSource.microphone()` builder yielding `source.buffers()` where each `buffer.bytes` supports `withUnsafeBytes` over its raw `s16le` bytes (identical to `swift/audio/Sources/audio/main.swift`).
- Produces: the executable's runtime behavior — a container entrypoint that logs `[<timestamp>] <text>` per transcription and appends the same line to `TRANSCRIPT_FILE`. Task 3's Dockerfile `CMD` runs it; Task 4's `wendy.json` provides the `/data` mount.

- [ ] **Step 1: Write `main.swift`**

File `swift/whisper-stt/Sources/whisper-stt/main.swift`:
```swift
// Headless continuous speech-to-text: gstreamer-swift mic capture (16 kHz mono
// s16le) → fixed chunks → RMS silence gate → in-process whisper.cpp inference →
// stdout + transcript file. The Swift port of python/whisper-stt.
internal import Foundation
import GStreamer
import CWhisper

#if canImport(Glibc)
import Glibc
#elseif canImport(Darwin)
import Darwin
#endif

// Line-buffer stdout so `wendy device logs` sees each transcription promptly
// (Swift 6 fully-buffers stdout on Linux when not a TTY — see the
// swift-ros2-template-gotchas memory).
setvbuf(stdout, nil, _IOLBF, 0)

// MARK: - Configuration (environment-driven)

let env = ProcessInfo.processInfo.environment

let sampleRate = 16_000
let whisperModel = env["WHISPER_MODEL"] ?? "base.en"
let modelPath = env["WHISPER_MODEL_PATH"]
    ?? "/opt/whisper/models/ggml-\(whisperModel).bin"
let language = env["WHISPER_LANGUAGE"] ?? "en"
let chunkSeconds = Double(env["CHUNK_SECONDS"] ?? "5.0") ?? 5.0
let silenceThreshold = Float(env["SILENCE_THRESHOLD"] ?? "0.01") ?? 0.01
let transcriptFile = env["TRANSCRIPT_FILE"] ?? "/data/transcript.txt"
let samplesPerChunk = Int(chunkSeconds * Double(sampleRate))

// MARK: - Errors

enum WhisperError: Error, CustomStringConvertible {
    case initFailed(String)
    var description: String {
        switch self {
        case .initFailed(let path): return "failed to load whisper model at \(path)"
        }
    }
}

// MARK: - Inference (owns the non-thread-safe whisper context)

actor Transcriber {
    private let ctx: OpaquePointer

    init(modelPath: String) throws {
        var cparams = whisper_context_default_params()
        cparams.use_gpu = true
        guard let ctx = whisper_init_from_file_with_params(modelPath, cparams) else {
            throw WhisperError.initFailed(modelPath)
        }
        self.ctx = ctx
    }

    func transcribe(_ samples: [Float], language: String) -> String {
        var params = whisper_full_default_params(WHISPER_SAMPLING_GREEDY)
        params.print_realtime = false
        params.print_progress = false
        params.print_timestamps = false
        params.no_context = true
        params.n_threads = Int32(max(1, ProcessInfo.processInfo.activeProcessorCount))

        return language.withCString { lang -> String in
            params.language = lang
            let rc = samples.withUnsafeBufferPointer { buf in
                whisper_full(ctx, params, buf.baseAddress, Int32(buf.count))
            }
            guard rc == 0 else { return "" }
            var text = ""
            for i in 0..<whisper_full_n_segments(ctx) {
                if let seg = whisper_full_get_segment_text(ctx, i) {
                    text += String(cString: seg)
                }
            }
            return text.trimmingCharacters(in: .whitespacesAndNewlines)
        }
    }

    deinit { whisper_free(ctx) }
}

// MARK: - Helpers

func rms(_ samples: [Float]) -> Float {
    guard !samples.isEmpty else { return 0 }
    let sumSq = samples.reduce(Float(0)) { $0 + $1 * $1 }
    return (sumSq / Float(samples.count)).squareRoot()
}

func timestamp() -> String {
    ISO8601DateFormatter().string(from: Date())
}

func appendLine(_ line: String, to path: String) {
    let fm = FileManager.default
    let dir = (path as NSString).deletingLastPathComponent
    if !dir.isEmpty {
        try? fm.createDirectory(atPath: dir, withIntermediateDirectories: true)
    }
    if !fm.fileExists(atPath: path) {
        fm.createFile(atPath: path, contents: nil)
    }
    guard let handle = FileHandle(forWritingAtPath: path) else { return }
    defer { try? handle.close() }
    handle.seekToEndOfFile()
    if let data = (line + "\n").data(using: .utf8) {
        handle.write(data)
    }
}

// MARK: - Main

let transcriber = try Transcriber(modelPath: modelPath)

let source = try AudioSource.microphone()
    .withSampleRate(sampleRate)
    .withChannels(1)
    .withFormat(.s16le)
    .build()

print("[whisper-stt] model=\(modelPath) lang=\(language) chunk=\(chunkSeconds)s threshold=\(silenceThreshold)")
print("[whisper-stt] transcript=\(transcriptFile)")
print("[whisper-stt] Listening...")

var pcm: [Float] = []
pcm.reserveCapacity(samplesPerChunk * 2)

for await buffer in source.buffers() {
    buffer.bytes.withUnsafeBytes { raw in
        let count = raw.count / MemoryLayout<Int16>.size
        for i in 0..<count {
            let sample = raw.loadUnaligned(fromByteOffset: i * MemoryLayout<Int16>.size, as: Int16.self)
            pcm.append(Float(sample) / 32768.0)
        }
    }

    while pcm.count >= samplesPerChunk {
        let chunk = Array(pcm[0..<samplesPerChunk])
        pcm.removeFirst(samplesPerChunk)

        if rms(chunk) < silenceThreshold { continue }

        let text = await transcriber.transcribe(chunk, language: language)
        guard !text.isEmpty else { continue }

        let line = "[\(timestamp())] \(text)"
        print(line)
        appendLine(line, to: transcriptFile)
    }
}
```

- [ ] **Step 2: Verify the file exists and is non-trivial**

Run: `wc -l swift/whisper-stt/Sources/whisper-stt/main.swift`
Expected: ~130 lines.

- [ ] **Step 3: Commit**

```bash
git add swift/whisper-stt/Sources/whisper-stt/main.swift
git commit -m "feat: add whisper-stt transcriber node (capture + in-process whisper.cpp)"
```

---

### Task 3: Dockerfile (primary build risk)

**Files:**
- Create: `swift/whisper-stt/Dockerfile`

**Interfaces:**
- Consumes: `Package.swift` + `Sources/` from Tasks 1–2.
- Produces: a runtime image whose `CMD` is the `{{.APP_ID}}` binary, with `libwhisper.so`/`libggml*.so`, the Swift runtime, the GStreamer/ALSA runtime plugins, and the baked model all present. Task 6 builds and runs it.

- [ ] **Step 1: Write the `Dockerfile`**

File `swift/whisper-stt/Dockerfile`:
```dockerfile
# syntax=docker/dockerfile:1.6
# Stage 1: build libwhisper.so (CUDA) + the Swift binary + bake the GGML model.
# Base matches swift/llm-gguf (CUDA present for the whisper.cpp GGML_CUDA build).
FROM dustynv/tensorrt:8.6-r36.2.0 AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates clang cmake curl git make ninja-build pkg-config \
    libcurl4-openssl-dev libedit2 libicu-dev libsqlite3-0 libxml2 \
    libncurses6 libstdc++6 libgcc-s1 \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
    && rm -rf /var/lib/apt/lists/*

# Swift toolchain (aarch64) — same approach as swift/llm-gguf.
RUN mkdir -p /opt/swift \
 && curl -fSL https://download.swift.org/swift-6.2.3-release/ubuntu2204-aarch64/swift-6.2.3-RELEASE/swift-6.2.3-RELEASE-ubuntu22.04-aarch64.tar.gz \
    | tar -xz -C /opt/swift --strip-components=1
ENV PATH="/opt/swift/usr/bin:${PATH}"

# whisper.cpp — pinned clone (NOT a vendored tarball), CUDA build, shared libs.
ARG WHISPER_CPP_TAG=v1.7.4
ARG CUDA_ARCH=87
RUN git clone --depth 1 --branch ${WHISPER_CPP_TAG} \
      https://github.com/ggerganov/whisper.cpp /opt/whisper.cpp \
 && cmake -S /opt/whisper.cpp -B /opt/whisper.cpp/build -G Ninja \
      -DGGML_CUDA=ON \
      -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH} \
      -DBUILD_SHARED_LIBS=ON \
      -DWHISPER_BUILD_EXAMPLES=OFF \
      -DWHISPER_BUILD_TESTS=OFF \
      -DCMAKE_INSTALL_PREFIX=/usr/local \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_SHARED_LINKER_FLAGS="-L/usr/local/cuda/lib64/stubs -lcuda" \
 && cmake --build /opt/whisper.cpp/build -j"$(nproc)" \
 && cmake --install /opt/whisper.cpp/build \
 && ldconfig

# Bake the GGML model into the image (default base.en).
ARG WHISPER_MODEL=base.en
RUN mkdir -p /opt/whisper/models \
 && bash /opt/whisper.cpp/models/download-ggml-model.sh ${WHISPER_MODEL} /opt/whisper/models

WORKDIR /app
COPY Package.swift ./
COPY Sources ./Sources
RUN --mount=type=cache,id=swiftpm-{{.APP_ID}},target=/app/.build \
    swift build -c release \
      -Xcc -I/usr/local/include \
      -Xlinker -L/usr/local/lib \
 && cp .build/release/{{.APP_ID}} /app/{{.APP_ID}}

# Stage 2: slim runtime — Swift runtime + whisper/ggml libs + GStreamer/ALSA + model.
FROM dustynv/tensorrt:8.6-r36.2.0

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libcurl4 libedit2 libicu70 libxml2 \
    libncurses6 libstdc++6 libgcc-s1 \
    libgstreamer1.0-0 gstreamer1.0-tools \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-alsa alsa-utils \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/swift/usr/lib/swift /opt/swift/usr/lib/swift
COPY --from=builder /usr/local/lib/libwhisper.so* /usr/local/lib/
COPY --from=builder /usr/local/lib/libggml*.so* /usr/local/lib/
COPY --from=builder /opt/whisper/models /opt/whisper/models
COPY --from=builder /app/{{.APP_ID}} /usr/local/bin/{{.APP_ID}}
RUN ldconfig

ENV LD_LIBRARY_PATH="/opt/swift/usr/lib/swift/linux:/usr/local/lib:${LD_LIBRARY_PATH}"
ENV WHISPER_MODEL={{.WHISPER_MODEL}}
ENV WHISPER_MODEL_PATH=/opt/whisper/models/ggml-{{.WHISPER_MODEL}}.bin
ENV TRANSCRIPT_FILE=/data/transcript.txt

CMD ["{{.APP_ID}}"]
```

- [ ] **Step 2: Commit**

```bash
git add swift/whisper-stt/Dockerfile
git commit -m "feat: add CUDA whisper.cpp multi-stage Dockerfile for swift whisper-stt"
```

---

### Task 4: App config, metadata, README

**Files:**
- Create: `swift/whisper-stt/wendy.json`
- Create: `swift/whisper-stt/template.json`
- Create: `swift/whisper-stt/README.md`

**Interfaces:**
- Consumes: the binary/behavior from Tasks 2–3.
- Produces: a renderable Wendy template `wendy init --template whisper-stt --language swift` can scaffold, mounting `/data` for the transcript.

- [ ] **Step 1: Write `wendy.json`**

File `swift/whisper-stt/wendy.json` (the `persist` block is copied verbatim from `swift/persistent-volume`):
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
            "type": "audio"
        },
        {
            "type": "gpu"
        },
        {
            "type": "persist",
            "name": "transcript",
            "path": "/data"
        }
    ]
}
```

- [ ] **Step 2: Write `template.json`**

File `swift/whisper-stt/template.json`:
```json
{
    "name": "whisper-stt",
    "description": "Headless Whisper speech-to-text in Swift (whisper.cpp, GPU on Jetson): USB mic → transcript file",
    "variables": [
        {
            "name": "APP_ID",
            "description": "Application identifier",
            "type": "string",
            "required": true,
            "prompt": "App ID"
        },
        {
            "name": "WHISPER_MODEL",
            "description": "Whisper GGML model baked into the image (e.g. tiny.en, base.en, small.en)",
            "type": "string",
            "default": "base.en",
            "prompt": "Whisper model"
        }
    ]
}
```

- [ ] **Step 3: Write the template `README.md`**

File `swift/whisper-stt/README.md`:
```markdown
# whisper-stt (Swift)

Headless speech-to-text in Swift. Captures audio from a USB microphone via
[gstreamer-swift](https://github.com/wendylabsinc/gstreamer-swift), transcribes
it continuously with [whisper.cpp](https://github.com/ggerganov/whisper.cpp)
(GPU-accelerated on NVIDIA Jetson), prints each transcription with a timestamp,
and appends it to a transcript file on a persistent volume.

The Swift port of `python/whisper-stt`. whisper.cpp is linked **in-process**
through a small C-interop target (`CWhisper`) — no shelling out.

## Deploy

```sh
wendy run --device <device> -y --detach
```

## See it work

```sh
wendy device logs --device <device>
```

Speak into the USB mic; you should see `[<timestamp>] <transcribed text>` lines.
The same lines are appended to `/data/transcript.txt`, which survives restarts.

## Configuration

| Variable            | Default                                   | Purpose                                             |
|---------------------|-------------------------------------------|-----------------------------------------------------|
| `WHISPER_MODEL`     | `base.en`                                 | GGML model baked into the image (template variable).|
| `WHISPER_MODEL_PATH`| `/opt/whisper/models/ggml-<model>.bin`    | Override the model file at runtime.                 |
| `WHISPER_LANGUAGE`  | `en`                                      | Transcription language.                             |
| `CHUNK_SECONDS`     | `5.0`                                      | Audio captured before each transcription.           |
| `SILENCE_THRESHOLD` | `0.01`                                     | RMS below which a chunk is skipped as silence.       |
| `TRANSCRIPT_FILE`   | `/data/transcript.txt`                    | Where transcriptions are appended (persistent).      |

## Compute

Built for NVIDIA Jetson: whisper.cpp is compiled with CUDA (`GGML_CUDA`) on the
same JetPack base image as the `llm-gguf` template. The image requires a
CUDA-capable runtime (any Jetson) to start; it is **not** a pure-CPU image and
will not run on a device with no CUDA runtime (e.g. a Raspberry Pi).
```

- [ ] **Step 4: Validate all JSON parses**

Run:
```bash
python3 -c "import json; [json.load(open(f)) for f in ['swift/whisper-stt/wendy.json','swift/whisper-stt/template.json']]" && echo OK
```
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add swift/whisper-stt/wendy.json swift/whisper-stt/template.json swift/whisper-stt/README.md
git commit -m "feat: add app config, metadata, and README for swift whisper-stt"
```

---

### Task 5: Register in `meta.json` and root `README.md`

**Files:**
- Modify: `meta.json` (the `whisper-stt` entry)
- Modify: `README.md` (the `whisper-stt` section)

**Interfaces:**
- Consumes: the template dir from Tasks 1–4.
- Produces: discoverability — `wendy init` lists Swift for `whisper-stt`; the repo README documents it.

- [ ] **Step 1: Update the `meta.json` entry**

In `meta.json`, change the `whisper-stt` entry's `languages` from `["python"]` to `["python", "swift"]`:
```json
        {
            "name": "whisper-stt",
            "description": "Headless Whisper speech-to-text on Jetson (USB mic → transcript file)",
            "languages": ["python", "swift"]
        }
```

- [ ] **Step 2: Verify `meta.json` still parses**

Run:
```bash
python3 -c "import json; d=json.load(open('meta.json')); print([t for t in d['templates'] if t['name']=='whisper-stt'][0]['languages'])"
```
Expected: `['python', 'swift']`

- [ ] **Step 3: Update the root `README.md` `whisper-stt` section**

Locate the `whisper-stt` section in `README.md` and add a Swift row to its language/framework table (or add the section if only Python is documented). Use this row, matching the table format already used by other multi-language sections in the file:
```markdown
| Swift | gstreamer-swift + whisper.cpp (in-process, CUDA) | `swift/whisper-stt/` |
```
(First read the existing `whisper-stt` section to match its exact table header/columns; if the section documents only Python as prose, convert it to the same table other Swift-ported templates use.)

- [ ] **Step 4: Commit**

```bash
git add meta.json README.md
git commit -m "docs: register swift whisper-stt in meta.json and README"
```

---

### Task 6: Render + build validation (the real test)

Templates can't build in place (they hold `{{.APP_ID}}` / `{{.WHISPER_MODEL}}`
tokens). Render a copy into the scratchpad, substitute tokens, rename the source
dir to the app id, and `docker build`. **Expect to iterate on the Dockerfile
and the `CWhisper` module map here** — the CUDA whisper.cpp build, the C-interop
include/link flags, and which `libggml*.so` files the runtime needs are the
risks. Fix issues in the real template files under `swift/whisper-stt/`, then
re-render and rebuild.

**Scratchpad:** `/private/tmp/claude-501/-Users-joannisorlandos-git-wendy-templates/1a971929-8e47-4cf8-9d94-acad54b98861/scratchpad`

> Build note: the `dustynv/tensorrt` builder image is large and compiles CUDA
> kernels for `sm_87`; the build succeeds without a local GPU (nvcc
> cross-compiles device code), but a GPU-less host **cannot run** the resulting
> image. Transcription is therefore verified on-device (Step 4). A clean
> `docker build` is the authoritative local gate (it proves compile + link + C
> interop + model bake).

- [ ] **Step 1: Render the template into the scratchpad with tokens substituted**

Run (renders from committed, tracked files — faithful to what `wendy init` ships):
```bash
SCRATCH="/private/tmp/claude-501/-Users-joannisorlandos-git-wendy-templates/1a971929-8e47-4cf8-9d94-acad54b98861/scratchpad"
DEST="$SCRATCH/whisper-render"
rm -rf "$DEST" && mkdir -p "$DEST"
git archive HEAD:swift/whisper-stt | tar -x -C "$DEST"
# Substitute tokens in every text file.
grep -rl -e '{{.APP_ID}}' -e '{{.WHISPER_MODEL}}' "$DEST" | while read -r f; do
  sed -i '' -e 's/{{\.APP_ID}}/whisperstt/g' -e 's/{{\.WHISPER_MODEL}}/base.en/g' "$f"
done
# Rename the source dir to the rendered target name.
mv "$DEST/Sources/whisper-stt" "$DEST/Sources/whisperstt"
echo "--- rendered Package.swift ---"; grep -n 'name:' "$DEST/Package.swift"
echo "--- token scan (should be empty) ---"; grep -rn '{{' "$DEST" || echo "no tokens remain"
```
Expected: `Package.swift` shows `name: "whisperstt"`; no `{{` tokens remain.

- [ ] **Step 2: Build the image**

Run:
```bash
docker build -t whisperstt "$DEST"
```
Expected: image builds. **If it fails**, likely culprits and fixes (apply to the real `swift/whisper-stt/` files, then re-render + rebuild):
- *`'whisper.h' file not found`* → the `swift build` `-Xcc -I/usr/local/include` flag isn't reaching the `CWhisper` clang import, or `cmake --install` didn't place headers. Add `RUN ls /usr/local/include/whisper.h` after install to confirm; ensure the flag is present.
- *link error `-lwhisper` / undefined `ggml_*`* → confirm `cmake --install` placed `libwhisper.so` and `libggml*.so` in `/usr/local/lib`; ensure `-Xlinker -L/usr/local/lib`. If ggml symbols are undefined at link, add `-Xlinker -lggml -Xlinker -lggml-base` to the `swift build`.
- *runtime `libggml*.so: cannot open shared object`* → widen the runtime `COPY --from=builder /usr/local/lib/libggml*.so*` glob (whisper 1.7.x splits ggml into `libggml`, `libggml-base`, `libggml-cpu`, `libggml-cuda`); confirm all landed and `ldconfig` ran.
- *`download-ggml-model.sh` bad args* → older/newer scripts vary; if the 2-arg form fails, `cd /opt/whisper.cpp && bash ./models/download-ggml-model.sh ${WHISPER_MODEL}` then move `models/ggml-*.bin` to `/opt/whisper/models/`.
- *wrong `WHISPER_CPP_TAG` (API drift)* → if `whisper_full_default_params`/`whisper_context_default_params` signatures don't match, pin a different tag; `v1.7.4` is the intended baseline.

- [ ] **Step 3: Confirm the binary and model are in the image**

Run:
```bash
docker run --rm --entrypoint sh whisperstt -c \
  'ls -la /usr/local/bin/whisperstt /opt/whisper/models/ && ls /usr/local/lib/libwhisper.so* /usr/local/lib/libggml*.so*'
```
Expected: the `whisperstt` binary, a `ggml-base.en.bin` model, and the whisper/ggml shared libs are all listed.

- [ ] **Step 4 (authoritative): deploy to a Jetson and confirm transcription**

Render via `wendy init` and deploy to a real device with a USB mic:
```bash
# in a scratch dir
wendy init --app-id whisperstt --template whisper-stt --language swift
cd whisperstt && wendy run --device <device> -y --detach
wendy device logs --device <device>   # speak into the mic; expect [<ts>] <text> lines
```
Expected: startup logs (`model=… Listening...`), then transcription lines as you speak; `/data/transcript.txt` accumulates the same lines. If no device is available, record that Step 2/3 passed and Step 4 is pending on-device verification.

- [ ] **Step 5: Commit any fixes made during iteration**

```bash
git add swift/whisper-stt
git commit -m "fix: make swift whisper-stt build and transcribe"
```

---

### Task 7: Update memory + open the PR

**Files:**
- Create/update: memory file capturing whisper.cpp Swift-binding gotchas (only non-obvious findings from Task 6).

- [ ] **Step 1: Record what was non-obvious**

If Task 6 surfaced non-obvious build facts (exact whisper.cpp tag with a stable C API, the precise `libggml*.so` set the runtime needs, C-interop include/link flags, the `download-ggml-model.sh` arg form), write `swift-whisper-cpp-template-gotchas.md` under the memory dir and add a one-line pointer to `MEMORY.md`. Link `[[swift-ros2-template-gotchas]]` and `[[swift-cxx-interop-template-gotchas]]`. Skip if nothing non-obvious came up.

- [ ] **Step 2: Push the branch and open the PR**

```bash
git push -u origin jo/samples-consolidation
gh pr create --base main --title "Add whisper-stt template in Swift" --body "$(cat <<'EOF'
## Summary

Adds `swift/whisper-stt` — a headless Swift port of the Python `whisper-stt`.

- Mic capture via `gstreamer-swift` (16 kHz mono s16le), the same path as `swift/audio`.
- In-process whisper.cpp inference through a `CWhisper` C-interop target — no `whisper-cli` shell-out.
- whisper.cpp built with `GGML_CUDA` on the `dustynv/tensorrt` Jetson base (matches `llm-gguf`); model baked at build time.
- Transcriptions logged and appended to `/data/transcript.txt` on a `persist` volume.
- Registered in `meta.json` (`whisper-stt` → `["python", "swift"]`) and documented in the root `README.md`.

Design + plan: `docs/superpowers/specs/2026-07-16-whisper-stt-swift-design.md`, `docs/superpowers/plans/2026-07-16-whisper-stt-swift.md`.

## Validation

Rendered the template and `docker build` succeeded (compile + link + C interop + model bake). [Update with on-device transcription results if run.]

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01M6BXEUMeCX8oWWhQzAxaRQ
EOF
)"
```
Expected: PR URL printed.

---

## Self-Review

**Spec coverage:**
- Binding approach (in-process libwhisper, CUDA Jetson base) → Global Constraints + Tasks 1, 3.
- Structure/layout → Tasks 1–4.
- Audio capture (gstreamer-swift, 16 kHz s16le, chunking, RMS gate) → Task 2.
- Inference (C interop, whisper_full, segment text) → Tasks 1, 2.
- whisper.cpp build (pinned clone, GGML_CUDA, no tarball) → Task 3.
- Model bake → Task 3; config surface → Task 2 + README (Task 4).
- Transcript persistence (persist entitlement) → Task 4.
- CPU-fallback caveat → README (Task 4).
- Repo integration (meta.json, README) → Task 5.
- Validation (render from tracked files, docker build, on-device) → Task 6.
- Memory + PR → Task 7.
All spec sections map to a task. No gaps.

**Placeholder scan:** No TBD/TODO. Every file's full contents are inline. The one bracketed note (`[Update with on-device …]`) is an intentional instruction to fill the PR body with real results, not a code placeholder. The README Step 5-3 asks the implementer to match an existing table format — that's reading real repo state, not a placeholder.

**Type consistency:** `Transcriber(modelPath:)`, `transcribe(_:language:)`, `rms(_:)`, `timestamp()`, `appendLine(_:to:)` are defined in Task 2 and used only there. `CWhisper` symbols (`whisper_init_from_file_with_params`, `whisper_context_default_params`, `whisper_full_default_params`, `WHISPER_SAMPLING_GREEDY`, `whisper_full`, `whisper_full_n_segments`, `whisper_full_get_segment_text`, `whisper_free`) are declared consumable in Task 1's Interfaces and used in Task 2. Target/binary name `{{.APP_ID}}` is consistent across Package.swift (Task 1), Dockerfile `cp`/`COPY`/`CMD` (Task 3), and the render/rename step (Task 6). Token names `{{.APP_ID}}`/`{{.WHISPER_MODEL}}` consistent across all files and the Task 6 render.
