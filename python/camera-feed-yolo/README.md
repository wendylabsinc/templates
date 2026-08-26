# {{.APP_ID}}

A Python camera viewer with YOLOv8 object detection. It uses CPU inference on a
generic WendyOS device and CUDA-enabled PyTorch on a supported NVIDIA Jetson.

## Requirements

- A reachable WendyOS device and Wendy CLI access
- A supported camera exposed as `/dev/video*`
- Network access during the first build to install packages and download the
  YOLOv8n weights
- Enough memory and disk for PyTorch, Ultralytics, and the selected build stage

The project declares camera, GPU, and host-network entitlements. The Wendy CLI
selects the generic or Jetson Docker stage from the target device capabilities.

## Run and verify

```sh
wendy run
```

The first build can take several minutes. Open
`http://<device-hostname>:{{.PORT}}`, select a camera, and adjust the confidence
control. The page uses `GET /cameras` and `WS /stream`; the WebSocket carries
MJPEG frames and detection metadata.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `APP_ID` | required | Application identifier |
| `PORT` | `3005` | HTTP/WebSocket listener, readiness probe, and UI port |

The readiness timeout is 180 seconds to allow for model and runtime startup.

## How it works

- `Dockerfile` selects a small CPU PyTorch base or a Jetson CUDA base with
  `WENDY_PLATFORM` and bakes `yolov8n.pt` into the image.
- `app.py` captures frames with GStreamer, runs Ultralytics YOLO tracking, and
  publishes JPEG frames with COCO detection metadata.
- `index.html` draws boxes and labels over the camera image.

## Extend it

- Change the baked model and model initialization in `Dockerfile` and `app.py`.
- Change capture settings in the GStreamer pipeline in `app.py`.
- Adjust tracking or confidence behavior in `app.py`, then update the matching
  controls in `index.html`.

Use `requirements.txt` for app dependencies. The container is the recommended
development environment because CPU and Jetson Python stacks differ.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 150
wendy device apps stop {{.APP_ID}}
```

If no video appears, check the selected V4L2 node and camera ownership. If model
loading or CUDA initialization fails, start with the first PyTorch or
Ultralytics message in the log; generic targets should use CPU inference.
