# Wendy Templates

Project templates for the [Wendy CLI](https://github.com/wendylabsinc/wendy-agent). Used by `wendy init --template` to scaffold new projects.

## Templates

### simple-api

A minimal HTTP API with JSON endpoints, ready to deploy to WendyOS.

| Language | Framework | Port | Directory |
|----------|-----------|------|-----------|
| Python | FastAPI 0.135.3 (uv + Python 3.14) | 3001 | `python/simple-api/` |
| Swift | Hummingbird 2.21.1 | 6001 | `swift/simple-api/` |
| Rust | Axum 0.8.8 | 4001 | `rust/simple-api/` |
| Node | TypeScript + Express | 5001 | `node/simple-api/` |
| C++ | Drogon 1.9.12 | 7001 | `cpp/simple-api/` |

Each template includes:
- `wendy.json` with network entitlement, TCP readiness probe, and postStart hook
- `Dockerfile` for containerized deployment
- Application source with `GET /`, `GET /health`, and `POST /items` endpoints

### common (planned)

Shared frontend templates that pair with any backend:

- `shadcn-react-typescript-frontend/` - React + TypeScript + shadcn/ui
- `raw-camera-feed-html/` - Minimal HTML page for camera streaming
- `raw-audio-feed-html/` - Minimal HTML page for audio streaming

## Template Tokens

Templates use placeholder tokens that are replaced by `wendy init`:

| Token | Description | Example |
|-------|-------------|---------|
| `{{APP_ID}}` | Application identifier | `my-app` |
| `{{PORT}}` | Primary HTTP port | `3001` |

## Usage

```bash
# Interactive - pick language and template from a list
wendy init

# Non-interactive
wendy init --app-id my-app --template simple-api --language python
```
