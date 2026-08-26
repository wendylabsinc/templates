# {{.APP_ID}}

A browser viewer for an IP camera already registered with WendyOS. The app reads
the platform-managed V4L2 loopback node and streams MJPEG to the browser; it does
not connect to the camera's RTSP endpoint itself.

## Requirements

- A reachable WendyOS device and Wendy CLI access
- An IP camera discovered by the device and registered with stored credentials
- WendyOS support for the platform camera loopback node (`/dev/video2xx`)

The loopback carries the platform-selected substream, normally limited to about
1024 pixels wide. Use the CLI `camera view` command when you only need to inspect
a registered camera; use this template when you need source code, a web UI, or
custom processing.

## Prepare the camera

```sh
wendy device camera list
wendy device camera login <camera-id>
wendy device camera view --id <camera-id>
```

The login command prompts for the camera password. The view command checks that
the platform can receive video before the application is deployed.

## Run and verify

```sh
wendy run
```

Open `http://<device-hostname>:{{.PORT}}`. The page should list a registered
camera node and show its stream. `GET /cameras` lists candidates and
`WS /stream` carries MJPEG frames and camera-switch messages.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `APP_ID` | required | Application identifier |
| `PORT` | `3005` | HTTP/WebSocket listener, readiness probe, and UI port |
| `CAMERA_DEVICE` | empty | Exact loopback node, such as `/dev/video203`; empty selects a capture-capable `/dev/video200`–`/dev/video255` node first |

`CAMERA_DEVICE` is rendered into the image. Regenerate or edit the project and
rebuild to change it.

## How it works

`wendy.json` grants host networking and camera access. `app.py` enumerates V4L2
capture nodes, prefers the platform-managed ID range, starts a GStreamer
pipeline for the selected node, and retries after pipeline loss. `index.html`
provides the browser viewer and camera selector.

## Extend it

- Change the candidate ordering or GStreamer pipelines in `app.py`.
- Add inference between capture and JPEG output, using the `camera-feed-yolo`
  project as a reference.
- Add camera status or controls to the WebSocket protocol and `index.html`.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 150
wendy device apps stop {{.APP_ID}}
```

If the CLI can view the camera but this app lists no suitable node, the target
likely lacks the loopback node. If several cameras exist, set `CAMERA_DEVICE` to
the node matching the desired camera ID. Stop any other app that holds the same
V4L2 node.
