# usb-camera — USB/UVC RGB webcam → HTTP MJPEG

Stream a plain USB RGB webcam from a Jetson (or any Linux device) as MJPEG over
HTTP. **No WebRTC, no AES key, no cloud** — it just opens a `/dev/video*` device
with OpenCV and serves the frames. Handy when the built-in camera path is gated
(e.g. the Go2 front camera's encrypted WebRTC handshake needs a per-device AES
key); plug in any UVC webcam and stream it instead.

## Endpoints

| path | what |
|------|------|
| `GET /` | live viewer page (the MJPEG in an `<img>`, with status) |
| `GET /stream` | `multipart/x-mixed-replace` MJPEG |
| `GET /stream/color` | alias of `/stream` — **drop-in for go2-rc's camera proxy** |
| `GET /health` | `{status, frames, fps, device, resolution, error}` |

## Deploy

```sh
wendy init --template usb-camera --app-id sh.wendy.usbcam
cd <app-id>
wendy run --device <jetson>
```

Then open **`http://<device>:<PORT>`** (default port `8000`). The `camera`
entitlement grants the container access to the host's `/dev/video*`.

## Use it as the Go2 camera (no AES key)

This serves `/stream/color`, the exact path `go2-rc`'s teleop proxies. To feed
this webcam into the go2-rc teleop UI instead of the (AES-gated) WebRTC camera,
point rc's `CAMERA_UPSTREAM_URL` at this service, e.g.
`http://127.0.0.1:8000/stream/color` if both run on the same device.

## Variables

| var | default | meaning |
|-----|---------|---------|
| `APP_ID` | — (required) | app identifier |
| `PORT` | `8000` | HTTP port |
| `VIDEO_DEVICE` | `/dev/video0` | V4L2 device (or an index like `0`) |
| `WIDTH` / `HEIGHT` | `1280` / `720` | capture resolution |
| `FPS` | `30` | target frame rate |
| `JPEG_QUALITY` | `80` | MJPEG quality (1–100) |

## Notes

- Most UVC webcams need **MJPG** for high res/fps; the app requests it. If a
  cam only does a lower mode, set `WIDTH`/`HEIGHT`/`FPS` to a supported combo
  (`v4l2-ctl --list-formats-ext` on the host shows what it supports).
- Multiple cameras: each shows up as a different `/dev/videoN`; set
  `VIDEO_DEVICE` accordingly (note UVC cams often claim two nodes — the capture
  one is usually the lower even number).
- The capture auto-reconnects if the camera is unplugged/replugged.
