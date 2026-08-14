# ip-camera-feed

Live view of a **platform-registered IP camera** in a browser: GStreamer
MJPEG-over-WebSocket, single service, no ML. This template consumes a camera
through the platform's camera-registration pipeline rather than dialing RTSP
itself — `rtspsrc` appears nowhere in this template by design. It is the
IP-camera counterpart of `python/camera-feed` (which targets a directly
attached USB webcam).

## How it works

The bare `{ "type": "camera" }` entitlement in `wendy.json` maps the
platform-managed `/dev/video2xx` loopback node into the container. Every
registered camera (device IDs 200-255, MAC-keyed) gets its own node at
`/dev/video<id>` on the device; a container holding the `camera` entitlement
sees whichever nodes exist automatically — nothing to wire up per container.

`app.py` auto-detects that node: it enumerates `/dev/video*`, keeps only
capture-capable nodes, and orders anything in the 200-255 band first (see
`CAMERA_DEVICE` below to pin an exact node instead).

> **The loopback node carries the camera's sub-stream** (<=1024px wide,
> auto-selected by the platform) — **there is no per-container stream
> selection**. It's right-sized for viewing and inference, not full-res
> recording.

## Configure & deploy

Register the camera and validate credentials before deploying:

```sh
wendy device camera list                 # shows discovered cameras (--refresh to re-scan)
wendy device camera login <id>           # store credentials for a camera
wendy device camera test <id>            # validate the stored credentials
wendy init --app-id my-cam --template ip-camera-feed --language python
cd my-cam
wendy run --device <device-hostname>
```

Then open `http://<device-hostname>:{{.PORT}}` and watch the feed.

## Requirements

- The camera must already be **registered** (`wendy device camera login`)
  before this template can see it — an unregistered camera has no loopback
  node.
- The target device's **WendyOS build must ship the `v4l2loopback` module**.
  On builds that don't, the `/dev/video2xx` node simply won't exist and this
  template will show no camera. `wendy device camera view <id>` still works
  from the CLI in that case and proves the camera itself is fine — the gap is
  the loopback node, not the camera or its credentials.

## Gotchas

- **Camera must be registered first.** `wendy device camera list` (optionally
  with `--refresh`) is how you confirm the platform sees it before deploying.
- **`CAMERA_DEVICE` override.** If a device has more than one registered
  camera and auto-detection doesn't pick the one you want, set the
  `CAMERA_DEVICE` template variable to the exact node (e.g. `/dev/video203`,
  matching the ID from `wendy device camera list`) at `wendy init` time, or
  leave it blank (the default) for auto-detection. This value is baked into
  the image as a Dockerfile `ENV` when the template is rendered, not read
  from the running container — to point a deployed app at a different
  camera, re-run `wendy init` with the new value and redeploy; editing the
  running container's environment has no effect. An invalid override (a
  node that isn't actually capture-capable, or doesn't exist) will still
  appear in the camera list, but the stream will never come up — the app
  retries forever rather than surfacing an error.
- No RTSP, no ML — just the MJPEG viewer. For object detection on a
  registered camera's feed, adapt `python/camera-feed-yolo`'s model-serving
  code onto this template's discovery.
