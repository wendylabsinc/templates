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

Live webcam streaming via GStreamer MJPEG over WebSocket. Entitlements: network (host), video, gpu.

### audio

Live audio waveform visualization with GStreamer mic capture. Streams raw PCM S16LE 16kHz mono over WebSocket. Includes sample .wav files for playback. Entitlements: network (host), audio.

### common

Shared building blocks (not selectable as templates):

- `shadcn-vite-frontend/` — Vite + React + shadcn/ui dashboard
- `camera-feed-html/` — Webcam viewer HTML page
- `audio-feed-html/` — Audio waveform visualizer HTML page

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
