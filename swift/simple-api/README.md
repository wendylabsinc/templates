# {{.APP_ID}}

A small Swift HTTP API built with Hummingbird. It provides a greeting, a health
check, and an example JSON endpoint for creating an item.

## Requirements

- A reachable WendyOS device
- Wendy CLI access to that device
- Network access during the first build to fetch Swift packages

No camera, audio device, or GPU is required. Local development uses Swift 6.3.

## Run

```sh
wendy run
```

The post-start hook opens `http://<device-hostname>:{{.PORT}}`. You can also
check the API directly:

```sh
curl http://<device-hostname>:{{.PORT}}/health
curl -X POST http://<device-hostname>:{{.PORT}}/items \
  -H 'content-type: application/json' \
  -d '{"name":"sensor","price":12.50}'
```

The health endpoint returns HTTP 200. The item endpoint returns the submitted
name and price with example ID `1`; it does not store the item.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `APP_ID` | required | Application, package, and executable name |
| `PORT` | `6001` | HTTP listener, readiness probe, and browser port |

`wendy.json` grants network access, waits for the TCP port for up to 30 seconds,
and opens the root URL after startup.

## How it works

- `Sources/{{.APP_ID}}/App.swift` defines `GET /`, `GET /health`, and
  `POST /items`.
- Hummingbird handles HTTP and Codable request/response bodies.
- The app includes request logging plus OpenTelemetry tracing and metrics
  middleware.
- `Dockerfile` creates a release binary and copies it into a Swift runtime.

## Extend it

Add route handlers and response types in `App.swift`. Split larger applications
into new source files, add storage, and update `wendy.json` for any additional
entitlements.

For local development:

```sh
swift run
```

## Operations

```sh
wendy device logs {{.APP_ID}} --tail 100
wendy device apps stop {{.APP_ID}}
```

If readiness times out, check that port `{{.PORT}}` is free and inspect the
first build or application error in the logs.
