# {{.APP_ID}}

A small Python HTTP API built with FastAPI. It provides a greeting, a health
check, and an example JSON endpoint for creating an item.

## Requirements

- A reachable WendyOS device
- Wendy CLI access to that device
- Network access during the first build to install Python packages

No camera, audio device, or GPU is required. The container uses Python 3.14.

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
| `APP_ID` | required | Application identifier |
| `PORT` | `3001` | HTTP listener, readiness probe, and browser port |

`wendy.json` grants network access, waits for the TCP port for up to 30 seconds,
and opens the root URL after startup.

## How it works

- `app.py` defines `GET /`, `GET /health`, and `POST /items`.
- FastAPI validates the item body with a Pydantic model.
- `Dockerfile` installs FastAPI and Uvicorn with `uv` and starts the server.

## Extend it

Add route functions and data models in `app.py`. Replace the fixed item response
with a database or another service, and add any required entitlement or
persistent volume to `wendy.json`.

For local development:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install fastapi 'uvicorn[standard]'
uvicorn app:app --host 0.0.0.0 --port {{.PORT}} --reload
```

## Operations

```sh
wendy device logs {{.APP_ID}} --tail 100
wendy device apps stop {{.APP_ID}}
```

If readiness times out, check that port `{{.PORT}}` is free and inspect the
first build or application error in the logs.
