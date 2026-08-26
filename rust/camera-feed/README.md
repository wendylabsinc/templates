# {{.APP_ID}}

A Rust camera viewer for WendyOS. It lists available V4L2 cameras and streams
MJPEG frames to a browser over WebSocket.

## Requirements

- A reachable WendyOS device and Wendy CLI access
- A supported USB or built-in camera exposed as `/dev/video*`
- Network access during the first build to fetch Rust crates

The project declares camera, GPU, and host-network entitlements. The GPU access
supports device-specific media paths; the application does not run an ML model.
Local development requires Rust, GStreamer development packages, and V4L2
tools.

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
| `APP_ID` | required | Application, package, and executable name |
| `PORT` | `4003` | HTTP/WebSocket listener, readiness probe, and UI port |

## How it works

`src/main.rs` owns camera discovery, a shared GStreamer pipeline, WebSocket
client tracking, and static-file responses. The pipeline starts when a viewer
connects and stops after the last viewer disconnects. `index.html` provides the
viewer and sends `switch_camera` messages when the selection changes.

## Extend it

- Change the GStreamer pipeline in `src/main.rs` to set resolution, frame rate,
  conversion, or encoding.
- Add capture metadata to the WebSocket protocol and render it in `index.html`.
- Add Axum routes and declare any new hardware access in `wendy.json`.

After installing the native GStreamer dependencies, run `cargo run` for local
development.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 100
wendy device apps stop {{.APP_ID}}
```

If no camera appears, confirm the device is connected and not held by another
application. A V4L2 node may expose metadata or infrared output instead of the
expected color stream; select another capture-capable node when available.
