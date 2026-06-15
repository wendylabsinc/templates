<p align="center">
  <img src="docs/media/demo.gif" alt="Wendy templates on NVIDIA Jetson" width="360">
</p>

# Wendy Templates

Project templates for the [Wendy CLI](https://github.com/wendylabsinc/wendy-agent). Used by `wendy init --template` to scaffold new projects.

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

### fullstack

Fullstack app with API backend + React/shadcn dashboard-01 frontend. Multi-stage Dockerfile builds the React frontend then serves it alongside a CRUD API for cars.

### camera-feed

Live webcam streaming via GStreamer MJPEG over WebSocket. Entitlements: network (host), camera, gpu.

### realsense-camera

Live Intel RealSense D415 multi-stream viewer: color, left IR, right IR, and colorized depth as MJPEG streams.

| Language | Framework | Default Port | Directory |
|----------|-----------|-------------|-----------|
| Python | FastAPI + pyrealsense2 | 8000 | `python/realsense-camera/` |
| C++ | Drogon + librealsense | 7007 | `cpp/realsense-camera/` |

The shared viewer frontend source lives at `common/realsense-camera-frontend/` and is vendored into both language template directories.

### audio

Live audio waveform visualization with GStreamer mic capture. Streams raw PCM S16LE 16kHz mono over WebSocket. Includes sample .wav files for playback. Entitlements: network (host), audio.

### voice-ai-pipecat

Always-on voice AI assistant: local [faster-whisper](https://github.com/SYSTRAN/faster-whisper) STT -> Gemini 2.5 Flash (with native Google Search grounding) -> local [Piper](https://github.com/rhasspy/piper) TTS, orchestrated by [Pipecat](https://github.com/pipecat-ai/pipecat). React visualizer ships two reactive line groups (blue = your voice, emerald = the bot). Entitlements: network (host), audio, gpu, persist (caches model weights at `/models`).

| Language | Framework | Default Port | Directory |
|----------|-----------|-------------|-----------|
| Python | Pipecat + FastAPI | 3005 | `python/voice-ai-pipecat/` |

The shared visualizer source lives at `common/voice-ai-pipecat-frontend/` and is vendored into the Python template directory.

### llm

Local LLM chat app with Ollama and Open WebUI, rebranded with Wendy assets. Entitlements: network (host), gpu, persist (model cache at `/models` and Open WebUI data at `/data`).

| Language | Framework | Default Port | Directory |
|----------|-----------|-------------|-----------|
| Python | Ollama + Open WebUI | 8080 | `python/llm/` |

Interactive init shows a model picker with `model`, `size`, `parameters`, and `comments` columns. Non-interactive init defaults to the smallest Gemma 4 option:

```bash
wendy init --app-id llm --target wendyos --language python --template llm --assistant skip --git-init no
```

### tailscale-bridge

Remote-access sidecar for devices behind residential ISP gateways or AP-isolated Wi-Fi networks. Many home routers enable AP isolation by default (clients cannot reach each other), and inbound port forwarding is often unavailable or impractical. This template ships a self-contained Alpine container that runs `tailscaled` in **userspace networking mode** (no `NET_ADMIN` cap, no TUN device required) and exposes the target app's dashboard to your tailnet via `tailscale serve` — zero router config, zero firewall rules.

The sidecar pairs with any other Wendy app that runs a local HTTP dashboard. Point `UPSTREAM_PORT` at that dashboard and the bridge makes it reachable from every device on your tailnet over HTTPS.

**Variables**

| Variable | Default | Description |
|---|---|---|
| `APP_ID` | *(required)* | Application identifier |
| `UPSTREAM_PORT` | `3001` | Local port of the app dashboard to reverse-proxy |
| `TS_HOSTNAME` | `wendy-bridge` | Tailnet machine name for this device |

**Auth key setup (one-time)**

The Tailscale auth key is not baked into the image. After deploying, paste a **reusable** auth key from the [Tailscale admin console](https://login.tailscale.com/admin/settings/keys) into the shared persist volume:

```bash
echo "tskey-auth-..." > /data/tailscale-authkey
```

The entrypoint polls `/data/tailscale-authkey` on startup and calls `tailscale up` once the file appears. The persist volume (`{{.APP_ID}}-tsdata` mounted at `/data`) survives restarts; the auth key only needs to be written once.

Tailscale node state is stored at `/data/state` on the same persist volume, so the device keeps its tailnet identity (machine key) across container restarts. Use a **reusable** auth key (not an ephemeral one): ephemeral keys register a new node on every `tailscale up`, but because the machine key persists here the node will simply re-authenticate without creating a duplicate entry.

**Directory:** `common/tailscale-bridge/` — the first standalone deployable template under `common/` (all other entries are vendored frontend assets).

### common

Shared building blocks (not selectable as templates):

- `tailscale-bridge/` — Userspace Tailscale remote-access sidecar (see above)
- `shadcn-vite-frontend/` — Vite + React + shadcn/ui dashboard
- `camera-feed-html/` — Webcam viewer HTML page
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
