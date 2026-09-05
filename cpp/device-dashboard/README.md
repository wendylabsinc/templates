# {{.APP_ID}}

A C++ and React starter for WendyOS. It serves a device dashboard, a persistent
SQLite CRUD API, system information, and optional camera, audio, and GPU pages
from one application.

## Requirements

- A reachable ARM64 WendyOS device and Wendy CLI access
- Network access during the first build for Drogon and frontend packages
- A camera or audio device only when using those pages
- An NVIDIA GPU only when using the GPU page

The application can run without optional device hardware. Its SQLite database
uses the persistent volume mounted at `/data`.

## Run and verify

```sh
wendy run
```

Open `http://<device-hostname>:{{.PORT}}`. The dashboard opens on the camera
page; select **System** in the sidebar to view system information. Check the
backend and persistent CRUD path with:

```sh
curl http://<device-hostname>:{{.PORT}}/api/system
curl -X POST http://<device-hostname>:{{.PORT}}/api/cars \
  -H 'content-type: application/json' \
  -d '{"make":"Wendy","model":"Demo","color":"blue","year":2026}'
curl http://<device-hostname>:{{.PORT}}/api/cars
```

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `APP_ID` | required | Application, executable, and persistent-volume name |
| `PORT` | `7001` | HTTP/WebSocket listener, readiness probe, and UI port |

`wendy.json` grants host networking, camera, audio, GPU, and persistent-storage
access. Remove unused entitlements when narrowing the project.

## How it works

- `main.cpp` implements the Drogon API, SQLite store, device discovery, and
  GStreamer camera/audio WebSockets.
- `frontend/src/` contains the React pages and navigation.
- `Dockerfile` builds the frontend and C++ server, then serves the compiled UI.
- `/api/cars` provides CRUD backed by `/data/cars.db`.
- `/api/system`, `/api/gpu`, `/api/cameras`, and `/api/microphones` report device
  state. `/api/camera/stream` and `/api/audio/stream` are WebSockets.

## Extend it

Add API handlers in `main.cpp`. Add a page under `frontend/src/pages/` and
register it in the frontend router. To develop the UI shell, run `npm install`
and `npm run dev` in `frontend/`; add a Vite proxy when connecting it to a
separately running backend.

Change the SQLite schema and queries together. Update `wendy.json` when a new
feature needs another device or storage entitlement.

## Operations

```sh
wendy device logs {{.APP_ID}} --tail 100
wendy device apps stop {{.APP_ID}}
```

If a device page is empty, check the relevant entitlement and device list. If
startup fails, check that `/data` is writable and port `{{.PORT}}` is free.
