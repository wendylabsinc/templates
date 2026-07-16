<p align="center">
  <img src="docs/media/demo.gif" alt="Wendy templates on NVIDIA Jetson" width="360">
</p>

# Wendy Templates

Project templates for the [Wendy CLI](https://github.com/wendylabsinc/wendy-agent). Used by `wendy init --template` to scaffold new projects.

> This repository supersedes the older [`wendylabsinc/samples`](https://github.com/wendylabsinc/samples) repo. Every sample worth keeping now lives here as a `wendy init --template` archetype; `samples` is being archived.

## Usage

```bash
# Interactive — pick template, language, and configure variables
wendy init

# Non-interactive
wendy init --app-id my-api --template simple-api --language rust --var PORT=9090

# Override any template variable
wendy init --app-id my-api --template simple-api --language python --var PORT=8080
```

## Available Templates

### simple-api

A minimal HTTP API with JSON endpoints (`GET /`, `GET /health`, `POST /items`), ready to deploy to WendyOS.

| Language | Framework | Default Port | Directory |
|----------|-----------|-------------|-----------|
| Python | FastAPI 0.135.3 (uv + Python 3.14) | 3001 | `python/simple-api/` |
| Swift | Hummingbird 2.21.1 | 6001 | `swift/simple-api/` |
| Rust | Axum 0.8.8 | 4001 | `rust/simple-api/` |
| Node | TypeScript + Express | 5001 | `node/simple-api/` |
| C++ | Drogon 1.9.12 | 7001 | `cpp/simple-api/` |

Each template includes:
- `wendy.json` — network entitlement, TCP readiness probe, postStart hook
- `Dockerfile` — containerized deployment
- Application source code

### persistent-volume

Minimal persistent volume demo: writes and reads `/data/foo.md` on startup to show that a mounted volume survives container restarts. No HTTP server. Entitlements: persist (`/data`).

| Language | Directory |
|----------|-----------|
| Python | `python/persistent-volume/` |
| Swift | `swift/persistent-volume/` |
| Rust | `rust/persistent-volume/` |
| Node | `node/persistent-volume/` |
| C++ | `cpp/persistent-volume/` |

### sqlite-persistence

SQLite on a persistent volume: appends a timestamped row and queries all rows on every startup, showing that data survives container restarts. Database lives at `/data/app.db`. No HTTP server. Entitlements: persist (`/data`).

| Language | Directory |
|----------|-----------|
| Python | `python/sqlite-persistence/` |
| Swift | `swift/sqlite-persistence/` |
| Rust | `rust/sqlite-persistence/` |
| Node | `node/sqlite-persistence/` |
| C++ | `cpp/sqlite-persistence/` |

### fullstack

Fullstack app with API backend + React/shadcn dashboard-01 frontend. Multi-stage Dockerfile builds the React frontend then serves it alongside a CRUD API for cars.

### webcam

Generic USB/UVC webcam viewer: enumerates `/dev/video*` and streams MJPEG-over-WebSocket via GStreamer, with multi-camera enumeration + switching. Entitlements: network (host), camera, gpu.

| Language | Framework | Default Port | Directory |
|----------|-----------|-------------|-----------|
| Python | FastAPI + GStreamer (PyGObject) | 3003 | `python/webcam/` |
| Swift | Hummingbird + gstreamer-swift | 3003 | `swift/webcam/` |
| Rust | Axum + GStreamer (gstreamer-rs) | 4003 | `rust/webcam/` |
| C++ | Drogon + GStreamer | 7003 | `cpp/webcam/` |
| Node | Express + ws + GStreamer (gst-launch-1.0 CLI) | 5003 | `node/webcam/` |

### realsense-camera

Live Intel RealSense D415 multi-stream viewer: color, left IR, right IR, and colorized depth as MJPEG streams.

| Language | Framework | Default Port | Directory |
|----------|-----------|-------------|-----------|
| Python | FastAPI + pyrealsense2 | 8000 | `python/realsense-camera/` |
| C++ | Drogon + librealsense | 7007 | `cpp/realsense-camera/` |

The shared viewer frontend source lives at `common/realsense-camera-frontend/` and is vendored into both language template directories.

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

### audio

Live audio waveform visualization with GStreamer mic capture. Streams raw PCM S16LE 16kHz mono over WebSocket. Includes sample .wav files for playback. Entitlements: network (host), audio.

### voice-ai-pipecat

Always-on voice AI assistant: local [faster-whisper](https://github.com/SYSTRAN/faster-whisper) STT -> Gemini 2.5 Flash (with native Google Search grounding) -> local [Piper](https://github.com/rhasspy/piper) TTS, orchestrated by [Pipecat](https://github.com/pipecat-ai/pipecat). React visualizer ships two reactive line groups (blue = your voice, emerald = the bot). Entitlements: network (host), audio, gpu, persist (caches model weights at `/models`).

| Language | Framework | Default Port | Directory |
|----------|-----------|-------------|-----------|
| Python | Pipecat + FastAPI | 3005 | `python/voice-ai-pipecat/` |

The shared visualizer source lives at `common/voice-ai-pipecat-frontend/` and is vendored into the Python template directory.

### llm

Local LLM chat app with Open WebUI.

On WendyOS, the Python template runs Ollama and Open WebUI as a **multi-service app group**: a standard `docker-compose.yml` defines the `ollama` and `open-webui` services, while a companion `wendy.json` adds the `appId` and GPU entitlement for Ollama.

| Target | Language | Framework | Default Port | Directory |
|--------|----------|-----------|-------------|-----------|
| WendyOS | Python | Ollama + Open WebUI | 8080 | `python/llm/` |

```bash
wendy init --app-id llm --target wendyos --language python --template llm --assistant skip --git-init no
```

### mac-llm

Native macOS MLX LLM chat app with Open WebUI for Wendy Agent for Mac. The Swift template runs a native Apple MLX backend plus Open WebUI. `Brewfile.wendy` installs `uv` on the target Mac, and the Swift supervisor installs pinned Open WebUI app-locally with `uv`, starts the private MLX `/v1` API on localhost, and exposes Open WebUI on the LAN.

| Target | Language | Framework | Default Port | Directory |
|--------|----------|-----------|-------------|-----------|
| Wendy Agent for Mac | Swift | MLX LLM + Hummingbird + Open WebUI | 8080 | `swift/mac-llm/` |

```bash
wendy init --app-id mac-llm --target darwin --language swift --template mac-llm --assistant skip --git-init no
```

### mlx-llm-chat

Self-contained MLX LLM chat app with its own React/shadcn frontend — distinct from `llm` (Ollama + Open WebUI) and `mac-llm` (native macOS + Open WebUI, no Dockerfile). Ships a Dockerfile and runs as a normal WendyOS linux container with a `gpu` entitlement, so it builds and deploys like any other template rather than relying on the Mac-only Wendy Agent supervisor.

| Language | Framework | Default Port | Directory |
|----------|-----------|-------------|-----------|
| Python | mlx-lm (macOS/Metal) / transformers (Linux CUDA/CPU) + FastAPI, serving Qwen3-4B-4bit | 3009 | `python/mlx-llm-chat/` |

```bash
wendy init --app-id mlx-llm-chat --language python --template mlx-llm-chat --assistant skip --git-init no
```

### llm-chat

Swift LLM chat: Hummingbird server + React/shadcn frontend, with a `BACKEND` template variable selecting the inference engine — `gguf` (default) shells out to `llama-cli` (llama.cpp, GGUF weights); `mlx` uses MLX-Swift (`mlx-swift-lm`) in-process. Both backends run on the `dustynv/tensorrt` Jetson/CUDA base image as a normal WendyOS linux container, so they target NVIDIA Jetson (and other CUDA Linux) devices rather than Apple Silicon. Entitlements: gpu, network (host).

| Language | Backend | Framework | Default Port | Directory |
|----------|---------|-----------|-------------|-----------|
| Swift | `gguf` (default) | Hummingbird + llama.cpp (`llama-cli`, GGUF) | 6002 | `swift/llm-chat/` |
| Swift | `mlx` | Hummingbird + MLX-Swift (`mlx-swift-lm`) | 6002 | `swift/llm-chat/` |

```bash
wendy init --app-id llm-chat --language swift --template llm-chat --assistant skip --git-init no --var BACKEND=gguf
wendy init --app-id llm-chat --language swift --template llm-chat --assistant skip --git-init no --var BACKEND=mlx
```

### tensorrt-hello

Minimal Swift↔TensorRT binding demo: prints a `TensorShape` via `import TensorRT` (`tensorrt-swift`). No HTTP server. Built on the `dustynv/tensorrt` Jetson base image, so it targets NVIDIA Jetson devices. Entitlements: gpu.

| Language | Directory |
|----------|-----------|
| Swift | `swift/tensorrt-hello/` |

```bash
wendy init --app-id tensorrt-hello --language swift --template tensorrt-hello --assistant skip --git-init no
```

### tensorrt-llm

Swift TensorRT-LLM token-streaming demo: autoregressive token-by-token generation with a simulated KV-cache, streamed to stdout via `import TensorRTLLM`/`TensorRTNative` (`tensorrt-swift`). No HTTP server. Built on the `dustynv/tensorrt` Jetson base image, so it targets NVIDIA Jetson devices. Entitlements: gpu, network (host).

| Language | Directory |
|----------|-----------|
| Swift | `swift/tensorrt-llm/` |

```bash
wendy init --app-id tensorrt-llm --language swift --template tensorrt-llm --assistant skip --git-init no
```

### hello-pytorch

GPU sanity-check app: polls PyTorch every 2 seconds and prints CUDA (NVIDIA GPU), MPS (Apple Silicon GPU), and CPU availability plus the PyTorch version. No HTTP server. Entitlements: gpu.

Ships two Dockerfiles: the default `Dockerfile` is CPU-only (works everywhere, no GPU required); `Dockerfile.jetson-slim` targets NVIDIA Jetson devices and requires a JetPack-matched `TORCH_WHL_URL` build-arg (see `python/hello-pytorch/README-JETSON.md`).

| Language | Directory |
|----------|-----------|
| Python | `python/hello-pytorch/` |

### bluetooth-discovery

BLE scanner: discovers nearby Bluetooth devices with [bleak](https://github.com/hbldh/bleak) and streams them live to a web UI over SSE (`/events`), alongside a one-shot `/discovered` JSON endpoint. Entitlements: network (host), bluetooth (bluez).

| Language | Framework | Default Port | Directory |
|----------|-----------|-------------|-----------|
| Python | FastAPI + bleak + sse-starlette | 8000 | `python/bluetooth-discovery/` |
| Swift | Hummingbird + [wendylabsinc/bluetooth](https://github.com/wendylabsinc/bluetooth) (BlueZ) | 8000 | `swift/bluetooth-discovery/` |

### whisper-stt

Headless Whisper speech-to-text for NVIDIA Jetson: captures audio from a USB microphone and continuously transcribes it to a file on a JetPack / CUDA base image. No HTTP server. The Python build uses [OpenAI Whisper](https://github.com/openai/whisper) over ALSA/PortAudio; the Swift build uses [whisper.cpp](https://github.com/ggerganov/whisper.cpp) in-process (via a `CWhisper` C-interop target) with [gstreamer-swift](https://github.com/wendylabsinc/gstreamer-swift) mic capture, and appends transcriptions to `/data` on a persistent volume. Entitlements: network (host), audio, gpu (Swift adds persist).

| Language | Directory |
|----------|-----------|
| Python | `python/whisper-stt/` |
| Swift | `swift/whisper-stt/` |

### asr-nemotron

Streaming ASR web demo: captures audio from a USB microphone, uses [Silero VAD](https://github.com/snakers4/silero-vad) to detect speech, and transcribes it with NVIDIA's Nemotron streaming ASR model, with a live waveform visualization and transcription/log tabs in the web UI. Model weights are cached on a persistent volume. Entitlements: network (host), audio, gpu, persist (model-cache).

| Language | Framework | Default Port | Directory |
|----------|-----------|-------------|-----------|
| Python | FastAPI + NeMo + Silero VAD | 3004 | `python/asr-nemotron/` |

### deepstream-vision

Jetson vision app group: real-time object detection (DeepStream + YOLO11n, Prometheus metrics + MJPEG stream), optional Qwen3-VL scene descriptions for high-confidence detections, and a GPU metrics exporter (tegrastats), plus a static `monitor.html` dashboard you open locally that talks to all three services directly over CORS. A **3-service app group** defined by one native `wendy.json` `services` map; `detector` depends on `vlm` since it calls out to it for scene descriptions. Entitlements: gpu, network (host) on every service.

| Service | Port | Role |
|---------|------|------|
| `detector` | 9090 | DeepStream YOLO11n detection, Prometheus metrics (`/metrics`), MJPEG stream (`/stream`) |
| `vlm` | 8090 | Qwen3-VL-2B (INT4) scene descriptions (`/describe`) |
| `gpu-stats` | 9091 | tegrastats-derived GPU temperature/memory/utilization metrics (`/metrics`) |

| Language | Directory |
|----------|-----------|
| Python | `python/deepstream-vision/` |

Targets NVIDIA Jetson Orin devices (DeepStream + TensorRT). Ports 9090/8090/9091 are fixed service-mesh constants — `monitor.html` hardcodes them and is not itself served by any container, so it is not templated.

### ai-security-camera

AI security camera for NVIDIA Jetson: ingests one or more IP-camera RTSP streams (ONVIF auto-discovery or a pinned `cameras.json`/`CAMERA_URLS` list), runs DeepStream YOLO11n object detection + NvDCF tracking on the GPU, and raises debounced security events (with saved snapshots) for people/vehicles. Single web dashboard with MJPEG preview, event log, Prometheus metrics (`/metrics`), and events API (`/events`). Ported from samples PR #13. Entitlements: gpu, network (host), persist (data).

| Language | Framework | Default Port | Directory |
|----------|-----------|-------------|-----------|
| Python | Flask + DeepStream + pyds | 8080 | `python/ai-security-camera/` |

Camera configuration (RTSP URLs, ONVIF discovery toggle, credentials) is runtime config in `cameras.json`/env vars, not a template variable — there is no single hardcoded stream to parameterize.

### voice-assistant

Local, fully offline voice assistant: wake-word detection ("wendy" via [openWakeWord](https://github.com/dscripka/openWakeWord)) -> [OpenAI Whisper](https://github.com/openai/whisper) STT -> a local LLM with tool-calling support (stubbed light-control tools) -> [Piper](https://github.com/rhasspy/piper) TTS, all running on-device on an NVIDIA Jetson. No HTTP server, no cloud calls beyond initial model downloads. Includes a `run-mac.sh` helper for local development on macOS. Entitlements: network (host), audio, gpu.

**Distinct from `voice-ai-pipecat`**: `voice-ai-pipecat` is a Pipecat-orchestrated pipeline that calls out to Gemini 2.5 Flash for the LLM step and ships a React visualizer; `voice-assistant` keeps the entire pipeline local (local LLM, no cloud inference, no web UI) and is headless.

| Language | Directory |
|----------|-----------|
| Python | `python/voice-assistant/` |

### common

Shared building blocks (not selectable as templates):

- `shadcn-vite-frontend/` — Vite + React + shadcn/ui dashboard
- `audio-feed-html/` — Audio waveform visualizer HTML page
- `realsense-camera-frontend/` — React + Vite viewer for the `realsense-camera` template (color + IR + depth streams)
- `voice-ai-pipecat-frontend/` — React + Three.js visualizer for the `voice-ai-pipecat` template (blue mic lines + emerald bot lines)

---

## Hosted template sources

Every push mirrors this repo to a public, branch-namespaced clone at **[templates.wendy.dev](https://templates.wendy.dev/)**, so any branch's template sources are fetchable over plain HTTPS without cloning the repo:

```
https://templates.wendy.dev/<branch>/<path>
```

| URL | Serves |
|-----|--------|
| `https://templates.wendy.dev/` | 302 redirect to `/main/` |
| `https://templates.wendy.dev/main/python/simple-api/wendy.json` | that file on `main` |
| `https://templates.wendy.dev/<branch>/...` | the same path on any branch |

Deployment is handled by [`.github/workflows/deploy-templates.yml`](.github/workflows/deploy-templates.yml): on every push it `rsync`s the repo tree (minus `.git`/`.github`) to `gs://wendy-templates-public/<branch>/`; deleting a branch removes its prefix. Content is fronted by Cloud CDN with a 5-minute `max-age`, so updates go live within a few minutes. Auth is keyless via GitHub OIDC / Workload Identity Federation — no secrets in the repo. The backing infrastructure (GCS bucket, CDN backend, HTTPS load balancer, managed cert, DNS) is managed in Google Cloud and mirrors the `docs.wendy.dev` setup.

### Branch names with slashes

Slashes in a branch name (e.g. `max/foo/bar`) are preserved verbatim as URL path segments — `https://templates.wendy.dev/max/foo/bar/...` — and need no encoding. You don't have to worry about a deep branch clobbering a shallower one (e.g. `max/foo` vs `max/foo/bar`): Git itself forbids a branch and a path-prefix of it from existing at the same time (the directory/file ref conflict), so the `rsync --delete` of one branch can never overlap another's prefix.

---

## Creating Templates

Templates are plain project directories with a `template.json` manifest and Go [`text/template`](https://pkg.go.dev/text/template) syntax in the source files.

### Directory structure

```
{language}/{template-name}/
├── template.json          # Variable declarations (required)
├── wendy.json             # App config (rendered)
├── Dockerfile             # Container build (rendered)
└── ...                    # Source files (rendered)
```

Templates are organized by language at the top level (`python/`, `swift/`, `rust/`, `node/`, `cpp/`). Each template directory must contain a `template.json`.

### template.json

The manifest declares the template's variables — their types, defaults, prompts, and validation rules. The CLI reads this at runtime to present interactive prompts (Bubble Tea) or accept `--var KEY=VALUE` flags.

```json
{
    "name": "simple-api",
    "description": "Minimal HTTP API with FastAPI",
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
            "default": 3001,
            "prompt": "HTTP port",
            "validate": { "min": 1, "max": 65535 }
        }
    ]
}
```

#### Variable fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Variable name, referenced in templates as `{{.NAME}}` |
| `description` | string | no | Help text shown in prompts |
| `type` | string | yes | `"string"`, `"integer"`, or `"boolean"` |
| `default` | any | no | Default value (type must match `type`) |
| `required` | boolean | no | If true and no default, the CLI will prompt or error |
| `prompt` | string | no | Label shown in interactive mode |
| `validate` | object | no | Validation rules (see below) |

#### Validation rules

For `integer` variables:
```json
{ "min": 1, "max": 65535 }
```

For `string` variables:
```json
{ "pattern": "^[a-z][a-z0-9-]*$" }
```

### Template syntax

Files use Go [`text/template`](https://pkg.go.dev/text/template) syntax. Variables are accessed with a dot prefix:

```
{{.APP_ID}}          — string substitution
{{.PORT}}            — integer substitution (rendered as string)
```

Go template conditionals and logic are supported:

```
{{if .ENABLE_CORS}}
app.use(cors());
{{end}}
```

### How the CLI processes templates

1. Downloads the `wendylabsinc/templates` repo as a tarball from GitHub
2. Extracts `{language}/{template-name}/` into a temp area
3. Reads `template.json` to discover variables
4. For each variable: checks `--var NAME=VALUE` flags, falls back to Bubble Tea prompts (text input for strings/integers, confirm for booleans)
5. Renders every file (except `template.json`) through `text/template` with the collected values
6. Writes output to `./{app-id}/`, renames template-named directories to the app ID
7. Deletes `template.json` from the output
8. Optionally runs `git init`

### Special variables

`APP_ID` is always available — it comes from the `--app-id` flag or the interactive prompt. You do not need to declare it in `template.json` (but you can to customize the prompt text).

### Tips

- Keep `template.json` next to `wendy.json` and `Dockerfile` at the template root
- Test your templates by running `wendy init --template {name} --language {lang}` locally
- Avoid complex logic in templates — conditionals are supported but keep them minimal
- Use sensible defaults so non-interactive mode works out of the box

---

## Acknowledgements

Sample `.wav` audio files in the audio template are from [pdx-cs-sound/wavs](https://github.com/pdx-cs-sound/wavs). Thanks to the Portland State University CS Sound group for making these available.
