# camera-feed-yolo (Mojo + MAX)

Live webcam streaming with YOLOv8n COCO detection, MAX edition: `wendycam`
captures MJPEG from V4L2, `wendyvision` TurboJPEG-decodes and letterboxes
frames into a buffer shared zero-copy with Python, and a **hand-built
YOLOv8n MAX graph** (`model.py`) runs inference — MAX 26.5 has no detection
architectures and cannot ingest the ONNX export (`docs/mojo-max-port-findings.md`
MMF-001/002), so the network is reconstructed layer by layer in the MAX Graph
API from build-time-extracted ultralytics weights. Confidence filtering, NMS,
and box unletterboxing run in Mojo. Same endpoints and client protocol as the
`python/camera-feed-yolo` sibling: `/` UI, `/cameras`, `/logs`, `/debug`, and
the `/stream` WebSocket (per frame: meta JSON text message, then the binary
JPEG; inbound `{"switch_camera": id}` and `{"confidence": v}`).

One image serves every target (`mojo/llm` rationale): CPU MEF artifacts
(imgsz 224) and Jetson-Orin GPU MEFs (sm_87, imgsz 320, cross-compiled with
no GPU present via the driver's virtual-device targeting) are both baked at
image build. Inference defaults to **CPU even on Jetson**: MAX 26.5's conv
kernels run ~10-40x below par on the Orin iGPU (522 ms/frame vs 57 ms on the
same device's CPU — MMF-026). `YOLO_DEVICE=gpu` opts into the GPU path,
which works end to end; an unbaked device/imgsz combination compiles its
MEFs once on first boot (the readiness timeout allows 600 s).

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `APP_ID` | required | Application identifier |
| `PORT` | `9005` | HTTP/WebSocket listener and UI port |
| `YOLO_DEVICE` | `cpu` | `cpu`, `gpu` (opt-in, see MMF-026), or `probe` |
| `YOLO_IMGSZ` | 320 GPU / 224 CPU | Square inference size (multiple of 32) |
| `YOLO_MAX_FPS` | `3` | Inference rate cap (frames still stream at camera rate) |

`YOLO_DEVICE`/`YOLO_IMGSZ`/`YOLO_MAX_FPS` are runtime env vars; combinations
without a baked MEF trigger a one-time on-device graph compile needing
~6-7 GB free RAM (MMF-023).

## Notes

- The Docker build needs **>= 8 GB RAM** for the MEF compile stage: the four
  graph parts peak at up to ~6 GB each in the 26.5 compiler, memory it never
  frees — which is why compilation runs one part per process and why the
  device itself never compiles the CPU path (MMF-023).
- 1x1 convolutions are lowered to matmul (missing `RSCF_to_KNkni` layout
  kernel, MMF-021) and the 2x nearest upsample is spelled
  reshape→broadcast→reshape (`resize_nearest` diverges from torch semantics,
  MMF-022); both rewrites are numerically exact — the graph output matches
  the fused ultralytics model to ~2e-6.
- Inference is inline in the single-threaded loop, rate-capped by
  `YOLO_MAX_FPS`; frames keep streaming at camera rate with the latest meta
  attached (same behavior as the python sibling's async inference thread).
- Requires an MJPEG-capable USB camera (same wendycam limitation as
  `mojo/camera-feed`).

## Verified

- **Numerics (in-container, arm64):** graph output matches the fused
  ultralytics reference to max |Δ| 0.002 px box / 2e-6 class score on
  bus.jpg; 13.5 ms/frame at imgsz 320 on an M-series Docker VM CPU.
- **Jetson Orin Nano (WendyOS, JP7.2, Logitech Brio):** CPU path — 27 fps
  1280x720 MJPEG streaming with 57 ms inference at imgsz 224 interleaved
  (rate-capped at `YOLO_MAX_FPS`). GPU path — runs end to end at imgsz 320
  but 522 ms/frame on MAX 26.5's conv kernels (MMF-026), hence the CPU
  default. M-series-built CPU MEFs execute unmodified on Cortex-A78AE.
