# {{.APP_ID}}

A C++ multi-stream viewer for the Intel RealSense D415. It serves color, left
infrared, right infrared, and colorized depth as MJPEG streams in one React UI.

## Requirements

- A reachable WendyOS device and Wendy CLI access
- An Intel RealSense D415 connected over USB
- Network access and enough build time to compile librealsense, Drogon, and the
  React frontend

The project uses USB and host-network entitlements. The container builds the
required librealsense version from source; the target does not need a separate
SDK installation.

## Run and verify

Connect the D415 before starting the app:

```sh
wendy run
```

Open `http://<device-hostname>:{{.PORT}}` and press **Start**. The default view
enables all four streams. Resolution, frame rate, and depth preset changes
restart the camera pipeline.

Useful endpoints are:

- `GET /health` — stream names, running state, and measured stream rates
- `POST /start` and `POST /stop` — pipeline lifecycle
- `POST /config?width=1280&height=720&fps=30&preset=default` — stream profile
- `GET /stream/color`, `/stream/ir-left`, `/stream/ir-right`, and
  `/stream/depth` — MJPEG output

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `APP_ID` | required | Application and executable name |
| `PORT` | `7007` | SPA/API listener, readiness probe, and UI port |

## How it works

`main.cpp` owns the librealsense pipeline, JPEG encoding, lifecycle API, and
Drogon MJPEG responses. `src/` contains the React controls and viewer. The
Dockerfile builds the frontend into `static/` and serves it from the C++ app.

The pipeline supports the profiles offered by `src/App.tsx`; the D415 must
support the selected profile across color, depth, and both infrared streams.

## Extend it

- Add or constrain profiles in `src/App.tsx` and the configuration validation
  in `main.cpp` together.
- Change stream processing or add metadata in the RealSense pump in `main.cpp`.
- Add a new stream ID to both the backend stream map and frontend `StreamId` UI.

For frontend-only work, run `npm install` and `npm run dev`. Vite proxies the
API to the C++ backend on port `{{.PORT}}`.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 150
wendy device apps stop {{.APP_ID}}
```

If startup reports no device, reconnect the D415 and stop other applications
using it. If a profile is rejected, return to 1280×720 at 30 fps or 640×480 at
30 fps before trying other supported combinations.
