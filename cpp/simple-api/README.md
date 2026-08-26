# {{.APP_ID}}

A small C++ HTTP API built with Drogon. It provides a greeting, a health check,
and an example JSON endpoint for creating an item.

## Requirements

- A reachable WendyOS device with an ARM64 container runtime
- Wendy CLI access to that device
- Network access during the first build to fetch and build Drogon

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
| `APP_ID` | required | Application and executable name |
| `PORT` | `7001` | HTTP listener, readiness probe, and browser port |

`wendy.json` grants network access, waits for the TCP port for up to 30 seconds,
and opens the root URL after startup.

## How it works

- `main.cpp` defines `GET /`, `GET /health`, and `POST /items`.
- `CMakeLists.txt` builds a C++17 executable linked to Drogon.
- `Dockerfile` builds Drogon and the application, then copies the executable
  into a small runtime image.

## Extend it

Add routes in `main.cpp` with `registerHandler`. Replace the fixed item response
with a database or another service, and add any required entitlement or
persistent volume to `wendy.json`.

For local development, install Drogon and use:

```sh
cmake -S . -B build
cmake --build build
./build/{{.APP_ID}}
```

## Operations

```sh
wendy device logs {{.APP_ID}} --tail 100
wendy device apps stop {{.APP_ID}}
```

If readiness times out, check that port `{{.PORT}}` is free and inspect the
first build or application error in the logs.
