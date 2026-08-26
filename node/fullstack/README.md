# {{.APP_ID}}

A TypeScript and React starter for WendyOS. It serves a device dashboard, a
persistent SQLite CRUD API, system information, and optional camera, audio, and
GPU pages from one application.

## Requirements

- A reachable WendyOS device and Wendy CLI access
- Network access during the first build for npm packages
- A camera or audio device only when using those pages
- An NVIDIA GPU only when using the GPU page

The application can run without optional device hardware. Its SQLite database
uses the persistent volume mounted at `/data`. Local backend development
requires Node.js 22 or later and the GStreamer command-line tools.

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
| `APP_ID` | required | Application, npm package, and persistent-volume name |
| `PORT` | `5001` | HTTP/WebSocket listener, readiness probe, and UI port |

`wendy.json` grants host networking, camera, audio, GPU, and persistent-storage
access. Remove unused entitlements when narrowing the project.

## How it works

- `src/index.ts` implements the Express API, SQLite store, device discovery,
  and GStreamer child processes.
- `frontend/src/` contains the React pages and navigation.
- `Dockerfile` builds the frontend and backend, then serves both with Node.js.
- `/api/cars` provides CRUD backed by `/data/cars.db`.
- `/api/system`, `/api/gpu`, `/api/cameras`, and `/api/microphones` report device
  state. `/api/camera/stream` and `/api/audio/stream` are WebSockets.

## Extend it

Add Express routes in `src/index.ts`. Add a page under `frontend/src/pages/`
and register it in the frontend router. For local work, run `npm install` and
`npm run dev` at the project root; run the same commands in `frontend/` for the
UI shell. Add a Vite proxy when connecting the UI to a separate backend.

Change the SQLite schema and prepared statements together. Update `wendy.json`
when a new feature needs another device or storage entitlement.

## Operations

```sh
wendy device logs {{.APP_ID}} --tail 100
wendy device apps stop {{.APP_ID}}
```

If a device page is empty, check the relevant entitlement and device list. If
startup fails, check that `/data` is writable and port `{{.PORT}}` is free.
