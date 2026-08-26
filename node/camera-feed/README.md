# {{.APP_ID}}

A TypeScript camera viewer for WendyOS. It lists available V4L2 cameras and
streams MJPEG frames to a browser over WebSocket.

## Requirements

- A reachable WendyOS device and Wendy CLI access
- A supported USB or built-in camera exposed as `/dev/video*`
- Network access during the first build to install npm packages

The project declares camera, GPU, and host-network entitlements. The GPU access
supports device-specific media paths; the application does not run an ML model.
Local development requires Node.js 22, GStreamer, and V4L2 tools.

## Run and verify

Connect the camera before starting the app, then run:

```sh
wendy run
```

Open `http://<device-hostname>:{{.PORT}}`. Select a camera if more than one is
listed. The viewer should change from connecting to a live image. The API used
by the page is:

- `GET /cameras` — V4L2 nodes reported by `v4l2-ctl` (not capability-filtered)
- `WS /stream` — MJPEG frames and camera-switch commands

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `APP_ID` | required | Application and npm package name |
| `PORT` | `5003` | HTTP/WebSocket listener, readiness probe, and UI port |

## How it works

`src/index.ts` owns camera discovery, the GStreamer child process, WebSocket
client tracking, and HTTP responses. Capture starts when a viewer connects and
stops after the last viewer disconnects. `index.html` provides the viewer and
sends `switch_camera` messages when the selection changes.

## Extend it

- Change the GStreamer arguments in `src/index.ts` to set resolution, frame
  rate, conversion, or encoding.
- Add capture metadata to the WebSocket protocol and render it in `index.html`.
- Add Express routes and declare any new hardware access in `wendy.json`.

For local development:

```sh
npm install
npm run dev
```

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 100
wendy device apps stop {{.APP_ID}}
```

If no camera appears, confirm the device is connected and not held by another
application. A V4L2 node may expose metadata or infrared output instead of the
expected color stream; select another capture-capable node when available.
