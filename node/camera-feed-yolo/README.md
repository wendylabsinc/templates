# {{.APP_ID}}

A TypeScript camera viewer with YOLOv8 object detection. It runs ONNX Runtime on
the CPU of a generic WendyOS device or uses the CUDA execution provider on a
supported NVIDIA Jetson.

## Requirements

- A reachable WendyOS device and Wendy CLI access
- A supported camera exposed as `/dev/video*`
- Network access during the first build to install npm packages and export the
  YOLOv8n ONNX model
- Enough memory and disk for ONNX Runtime and the model build stages

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
| `APP_ID` | required | Application and npm package name |
| `PORT` | `5006` | HTTP/WebSocket listener, readiness probe, and UI port |

The readiness timeout is 180 seconds to allow for model and runtime startup.

## How it works

- `Dockerfile` exports `yolov8n.onnx` and selects a CPU or Jetson ONNX Runtime
  base with `WENDY_PLATFORM`.
- `src/index.ts` captures MJPEG with GStreamer, preprocesses 640-pixel
  letterboxed input, runs `onnxruntime-node`, filters detections, and publishes
  COCO labels.
- `index.html` draws boxes and labels over the camera image.

## Extend it

- Replace the model export in `Dockerfile`, then update tensor decoding and the
  label list in `src/index.ts` to match the new model.
- Change capture settings in the GStreamer argument builder.
- Extend the WebSocket metadata and its rendering in `index.html`.

Use `npm install` and `npm run dev` for local TypeScript work after installing
GStreamer and a compatible ONNX Runtime environment.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 150
wendy device apps stop {{.APP_ID}}
```

If no video appears, check the selected V4L2 node and camera ownership. If model
loading or CUDA initialization fails, start with the first ONNX Runtime message
in the log; generic targets should use the CPU stage.
