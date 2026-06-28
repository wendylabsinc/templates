# camera-feed-yolo-fire

Live camera feed with **YOLOv8 fire & smoke detection**. A USB/CSI camera is
captured via GStreamer (with an OpenCV fallback), every frame is streamed as
MJPEG over a WebSocket, and a custom YOLOv8 model runs in a background thread to
overlay `fire` / `smoke` / `other` bounding boxes on the live feed.

This is the [`camera-feed-yolo`](../camera-feed-yolo) template with the generic
COCO `yolov8n.pt` swapped for a purpose-trained **`fire.pt`** model (3 classes:
`fire`, `other`, `smoke`).

## How it works

- **Capture** — GStreamer on Jetson (HW JPEG), OpenCV on CPU/RPi (lighter idle).
- **Stream** — raw JPEG frames pushed over `/stream`; the browser draws boxes on
  a canvas overlay so slow inference never stalls the video.
- **Inference** — runs on its own thread, FPS-capped (`YOLO_MAX_FPS`, default 15
  on CUDA / 3 on CPU) and decoupled from the stream rate.
- **Model** — `fire.pt` is **baked into the image** at build time. It is a custom
  model (not on the ultralytics hub), so it ships in the template and is `COPY`d
  into the container — no network needed at runtime.

## Platform selection

The base image is chosen at `wendy run` time from the device probe:

- **Jetson** (`WENDY_PLATFORM=nvidia-jetson`) → `dustynv/pytorch` (CUDA), GStreamer capture, `imgsz=320`.
- **Generic / RPi** → `python:3.11-slim-bookworm` (CPU), OpenCV capture, `imgsz=224`.

## Configure

Prompts for:

- **APP_ID** — application identifier.
- **PORT** (default `3006`) — HTTP/WebSocket port.

## Run

```sh
wendy init --template camera-feed-yolo-fire --language python
cd <app-id>
wendy run --device <device-hostname>
```

Then open `http://<device-hostname>:3006`. The UI shows the live feed, FPS,
detection count, inference time, and a per-class tally. Use the **Conf** slider
(default `0.60`) to trade sensitivity against false positives, and the camera
dropdown to switch sources.

## Tuning (env vars)

| Var | Default | Purpose |
| --- | --- | --- |
| `YOLO_MAX_FPS` | `15` (CUDA) / `3` (CPU) | Inference rate cap. |
| `CAMERA_BACKEND` | `auto` | Force `opencv` or `gstreamer`. |
| `FIRE_MODEL` | `fire.pt` | Path to an alternate model (e.g. an exported `fire.onnx`). |

## Endpoints

- `GET /` — viewer UI
- `GET /cameras` — discovered cameras
- `WS /stream` — JPEG frames + JSON detection metadata; accepts `{switch_camera}` / `{confidence}`
- `GET /debug` — runtime state (model, CUDA, classes, last detections)
- `GET /logs` — recent log buffer
