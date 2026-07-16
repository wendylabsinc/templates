# Templates dedup + Swift ports — design

Date: 2026-07-16
Branch: `jo/samples-consolidation`

## Goal

Reduce duplication in the consolidated templates repo and extend Swift coverage:

1. **Consolidate the camera examples** — make `webcam` the single camera template, ported to all five languages, and delete `camera-feed`.
2. **Merge the two Swift LLM chat templates** (`swift/mlx-llm-chat` + `swift/llm-gguf`) into one template selected by a `BACKEND` variable.
3. **Port `bluetooth-discovery` to Swift** using the first-party `wendylabsinc/bluetooth` package.

Out of scope (explicitly left as-is): `swift/cuda-llm`, `whisper-stt`/`asr-nemotron`, and the untracked local WIP dirs `python/wifi-sensing`, `python/go2-quadruped`.

---

## Work item 1 — Camera consolidation (`webcam` everywhere, drop `camera-feed`)

### Current state
- `camera-feed` exists in all 5 languages (`python`, `swift`, `cpp`, `rust`, `node`). Transport: GStreamer **MJPEG over WebSocket**.
- `webcam` exists only in `python` + `swift`. Despite the "WebRTC with MJPEG fallback" wording in `meta.json`/`README.md`, **the actual implementation is also MJPEG-over-WebSocket** (Python `MJPEGCamera` + `/stream`; Swift `HummingbirdWebSocket` + `gstreamer-swift` appsink). `webcam` is a **superset** of `camera-feed`, adding:
  - camera enumeration endpoint (`GET /cameras`)
  - camera switching (WebSocket `{"switch_camera": <id>}` command)
  - `GET /logs` and `GET /debug` endpoints
  - native-MJPEG-preferred pipeline with `jpegenc` fallback (Jetson uses `nvjpegenc`)
  - macOS device support (Python path)

### Target
- `webcam` becomes the single camera template in **all 5 languages**.
- `cpp/webcam`, `rust/webcam`, `node/webcam` are created by extending each language's existing `camera-feed` (which already does GStreamer→WebSocket) with the webcam feature set above, plus the `webcam` `index.html` + `logo.svg`.
- `camera-feed` is deleted in all 5 languages.
- `camera-feed-yolo` is untouched (it remains the camera-capture example that also does inference).

### Feature parity bar for the three new ports
Each of `cpp/webcam`, `rust/webcam`, `node/webcam` must provide, at minimum:
- `GET /` → serves `index.html`; `GET /logo.svg`
- `WS /stream` → streams JPEG frames; accepts `{"switch_camera": id}`
- `GET /cameras` → JSON list of `{id, name}`
- MJPEG-native-preferred GStreamer pipeline with a `videoconvert ! jpegenc` fallback

`/logs` and `/debug` are ported where the existing `camera-feed` for that language already has a logging buffer; otherwise they are optional (documented as such in the plan). The Jetson `nvjpegenc` hardware path is included where the language's `camera-feed` already uses it.

### Repo-level changes
- `meta.json`: remove the `camera-feed` entry; drop the `languages` field on the `webcam` entry (implies all 5) and correct the description to "Generic USB/UVC webcam: GStreamer MJPEG over WebSocket with multi-camera enumeration + switching" (remove the inaccurate WebRTC claim).
- `README.md`: remove the `camera-feed` section; fix the `webcam` section (correct the WebRTC wording, add `cpp`/`rust`/`node` rows to its table). Verify the unrelated `camera-feed-html/` mention (line ~274) and leave it if it refers to a different asset.
- Grep the repo for any other `camera-feed` references and update.

### Risks
- Per-language GStreamer element availability (esp. `nvjpegenc` on Jetson vs `jpegenc` elsewhere) — keep the fallback ladder the Python/Swift versions use.
- Node/Rust/C++ GStreamer bindings differ; enumeration APIs vary. Enumeration is the main new surface beyond the existing `camera-feed` base.

---

## Work item 2 — Merge Swift LLM chats into one `BACKEND`-variable template

### Current state
Two near-identical-shaped templates, each = React (Vite/shadcn) frontend + Swift/Hummingbird server, differing only in inference backend:

| | `swift/mlx-llm-chat` | `swift/llm-gguf` |
|---|---|---|
| Server target | `mlx-llm-server` | `qwen3-chat-server` |
| Extra dep | `mlx-swift-lm` (MLX) | none (shells out to `llama-cli`) |
| Dockerfile base | `swift:6.2.3-noble` | `dustynv/tensorrt:8.6-r36.2.0` + builds llama.cpp w/ CUDA |
| Frontend `App.tsx` | differs | differs |

Known problems to fix as part of the merge:
- **MLX-Swift is Apple-Silicon/Metal only** — the `mlx-llm-chat` Docker image (Linux base + MLX dep) almost certainly does not build on a WendyOS/Jetson device. The `mlx` backend is therefore documented as a **macOS/Apple-Silicon** target; the `gguf` backend is the Linux/Jetson default.
- **`llm-gguf` references `llama.cpp.tar.gz`, which is not tracked in git** — the merged template must obtain llama.cpp reproducibly (pinned `git clone` of a tag in the Dockerfile) rather than a vendored tarball.

### Target
New template `swift/llm-chat` with a `BACKEND` variable, `enum: ["gguf", "mlx"]`, default `gguf`.

- `template.json`: add the `BACKEND` variable (with `prompt`, `default: "gguf"`, enum validation) alongside `APP_ID` + `PORT`.
- `template.schema.json` (if the template ships one): mirror `BACKEND` there too — per the known gotcha, a variable present only in the schema and not in `template.json` renders as `<no value>`.
- **Rendering:** Wendy uses Go `text/template`. The `Dockerfile` and `Package.swift` use `{{ if eq .BACKEND "gguf" }} … {{ else }} … {{ end }}` conditionals:
  - `Package.swift`: include the `mlx-swift-lm` dependency + product only in the `mlx` branch.
  - `Dockerfile`: `gguf` branch = TensorRT base + pinned llama.cpp CUDA build; `mlx` branch = `swift:*-noble` MLX build.
- **Server sources:** ship both target source dirs under `server/Sources/` (`llm-server-gguf`, `llm-server-mlx`); the rendered `Package.swift` + `Dockerfile` build only the selected one. (Alternative considered — one target with `#if` — rejected because the MLX import can't compile on Linux at all.)
- **Frontend:** reconcile to a single React frontend. Default to `llm-gguf`'s `App.tsx` (the working Jetson chat) unless review of the diff shows `mlx-llm-chat`'s is preferable; document the choice in the plan.

### Repo-level changes
- Delete `swift/mlx-llm-chat` and `swift/llm-gguf`; add `swift/llm-chat`.
- `meta.json`: remove the `llm-gguf` entry; add a `llm-chat` entry (`languages: ["swift"]`, description noting the `gguf`/`mlx` backends). The `mlx-llm-chat` entry becomes **python-only** (`languages: ["python"]`) since `python/mlx-llm-chat` stays and the Swift MLX variant now lives inside `llm-chat`.
- `README.md`: update the LLM section accordingly.

### Risks
- Conditional Dockerfiles are the fragile part; both backend branches must be build-tested independently after rendering.
- The `gguf` llama.cpp fetch must be pinned and CUDA-arch-parameterized (existing `ARG CUDA_ARCH=87`).

---

## Work item 3 — Port `bluetooth-discovery` to Swift

### Current state (Python)
Small app: `bleak` (BlueZ over D-Bus) + FastAPI. Endpoints: `GET /discovered` (5 s scan → JSON), `GET /events` (SSE stream of `{name, address, rssi}` for ~30 s), `GET /` → `index.html`. `wendy.json` entitlements: `network: host` + `bluetooth: bluez`. `readiness.tcpSocket` on `PORT` (default 8000) + `postStart` open-browser hook.

### Feasibility (confirmed)
- The `bluetooth: bluez` entitlement injects a **filtered `org.bluez` D-Bus system-bus socket** into the container (via `xdg-dbus-proxy`), sets `DBUS_SYSTEM_BUS_ADDRESS`, and grants **no** HCI/`/dev`/`CAP_NET_RAW`. So the Swift port must be a **BlueZ D-Bus client**, exactly like `bleak`.
- `wendylabsinc/bluetooth` (v0.0.2+) has a **fully implemented BlueZ D-Bus backend** (`BlueZScanController`, 480 lines) selected via the `backend_bluez` SwiftPM trait; it depends on `wendylabsinc/dbus` (libdbus-1). `CentralManager.scan(filter:parameters:)` returns `AsyncStream<ScanResult>` where:
  - `result.peripheral.id` → `.address(BluetoothAddress)` = MAC address
  - `result.peripheral.name ?? result.advertisementData.localName` = name
  - `result.rssi` = RSSI

  This is a 1:1 map to the Python `{name, address, rssi}`.

### Target
`swift/bluetooth-discovery`:
- **Server:** Hummingbird, mirroring `swift/webcam`'s layout. Routes:
  - `GET /` → `index.html`, `GET /logo.svg`
  - `GET /discovered` → run `CentralManager.scan` for 5 s, collect unique devices, return JSON array of `{name, address, rssi}`
  - `GET /events` → SSE (`text/event-stream`) streaming devices as they arrive from the scan `AsyncStream`, using Hummingbird's streaming `ResponseBody`, emitting `data: <json>\n\n` per device (bounded ~30 s to match Python).
- **Package.swift:** `hummingbird`, `wendylabsinc/bluetooth` (enable `backend_bluez` trait on Linux), `swift-container-plugin` (consistent with sibling templates).
- **Dockerfile:** two-stage Swift build (base `swift:6.2.3-noble`, matching non-CUDA Swift templates). Install `libdbus-1-dev` (build) and `libdbus-1-3` + `bluez` (runtime), mirroring the Python template's D-Bus/bluez setup. Copy binary + `index.html`/`logo.svg`. `EXPOSE {{.PORT}}`.
- **wendy.json:** copy the Python one verbatim (entitlements `network: host` + `bluetooth: bluez`, `readiness.tcpSocket` on `{{.PORT}}`, `postStart` open-browser). `appId: {{.APP_ID}}`.
- **template.json:** `name: bluetooth-discovery`, variables `APP_ID` (required) + `PORT` (default 8000), matching the Python template.
- **index.html / logo.svg:** reuse the Python template's assets (adjust only if endpoint shapes differ; they won't).

### Repo-level changes
- `meta.json`: `bluetooth-discovery` entry gains Swift → `languages: ["python", "swift"]`.
- `README.md`: add the Swift row/section for `bluetooth-discovery`.

### Risks
- SSE in Hummingbird: confirm the streaming-response mechanism (`ResponseBody`/`AsyncSequence` writer) and correct `Content-Type: text/event-stream` + no-buffering headers.
- The `backend_bluez` trait must be enabled for the Linux build (via Package.swift default traits or a `swift build --traits backend_bluez` in the Dockerfile). Verify which mechanism the package expects.
- Scan de-duplication (`allowDuplicates: false`) to avoid spamming `/events` with repeats.

---

## Sequencing & verification

The three items are independent and can land in any order (suggest: bluetooth port → LLM merge → camera port, easiest-to-hardest). Each template must be validated by rendering with the Wendy CLI and building its image before the corresponding `camera-feed`/`llm-gguf`/`mlx-llm-chat` deletions are committed. `meta.json` must remain valid JSON and stay in sync with the on-disk template dirs after each item.
