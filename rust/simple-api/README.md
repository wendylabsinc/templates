# {{.APP_ID}}

A small Rust HTTP API built with Axum. It provides a greeting, a health check,
and an example JSON endpoint for creating an item.

## Requirements

- A reachable WendyOS device
- Wendy CLI access to that device
- Network access during the first build to fetch Rust crates

No camera, audio device, or GPU is required.

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

The health response is `{"status":"ok"}`. The item endpoint returns the
submitted name and price with example ID `1`; it does not store the item.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `APP_ID` | required | Application, package, and executable name |
| `PORT` | `4001` | HTTP listener, readiness probe, and browser port |

`wendy.json` grants network access, waits for the TCP port for up to 30 seconds,
and opens the root URL after startup.

## How it works

- `src/main.rs` defines `GET /`, `GET /health`, and `POST /items`.
- Axum handles routing and Serde validates and serializes JSON.
- `Dockerfile` creates a release binary and copies it into a Debian runtime.

## Extend it

Add handlers and routes in `src/main.rs`. Split larger applications into
modules, replace the fixed item response with storage, and add any required
entitlement or persistent volume to `wendy.json`.

For local development:

```sh
cargo run
```

## Operations

```sh
wendy device logs {{.APP_ID}} --tail 100
wendy device apps stop {{.APP_ID}}
```

If readiness times out, check that port `{{.PORT}}` is free and inspect the
first build or application error in the logs.
