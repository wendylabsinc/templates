# {{.APP_ID}}

A Swift camera viewer for WendyOS. It lists available V4L2 cameras and streams
MJPEG frames to a browser over WebSocket.

## Requirements

- A reachable WendyOS device and Wendy CLI access
- A supported USB or built-in camera exposed as `/dev/video*`
- Network access during the first build to fetch Swift packages

The project declares camera, GPU, and host-network entitlements. The GPU access
supports device-specific media paths; the application does not run an ML model.
Local development requires Swift `{{.SWIFT_VERSION}}`, GStreamer, and V4L2
tools.

## Run and verify

Connect the camera before starting the app, then run:

```sh
wendy run
```

Open `http://<device-hostname>:{{.PORT}}`. Select a camera if more than one is
listed. The viewer should change from connecting to a live image. The API used
by the page is:

- `GET /cameras` — discovered V4L2 nodes (not capability-filtered)
- `WS /stream` — MJPEG frames and camera-switch commands

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `APP_ID` | required | Application, package, and executable name |
| `PORT` | `6003` | HTTP/WebSocket listener, readiness probe, and UI port |
| `SWIFT_VERSION` | `6.3` | Swift container toolchain tag |

## How it works

`Sources/{{.APP_ID}}/` contains camera discovery, the GStreamer process wrapper,
MJPEG parsing, WebSocket handling, and static-file middleware. Capture starts
when a viewer connects and stops after the last viewer disconnects.
`index.html` sends `switch_camera` messages when the selection changes.

## Extend it

- Change the GStreamer arguments in `MJPEGCamera.swift` to set resolution,
  frame rate, conversion, or encoding.
- Add capture metadata in `App.swift` and render it in `index.html`.
- Add Hummingbird routes and declare new hardware access in `wendy.json`.

Container deployment is the closest match to the WendyOS GStreamer and device
environment.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 100
wendy device apps stop {{.APP_ID}}
```

If no camera appears, confirm the device is connected and not held by another
application. A V4L2 node may expose metadata or infrared output instead of the
expected color stream; select another capture-capable node when available.
