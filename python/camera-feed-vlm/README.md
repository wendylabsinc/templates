# camera-feed-vlm

Live camera feed **+ vision-language chat**: a single FastAPI service streams the
webcam to the browser and lets you **ask questions about what the camera sees**.
Each question grabs the current frame, runs a vision-language model (VLM) on the
**GPU (CUDA on Jetson, CPU fallback elsewhere)**, and streams the answer back.

This template is a fork of [`camera-feed-yolo`](../camera-feed-yolo): it reuses
the entire GStreamer/OpenCV capture + MJPEG-over-WebSocket streaming layer and the
platform-selecting Dockerfile, swapping the YOLO detection head for an in-process
VLM and a chat panel.

## How it works

- **Video and inference are decoupled.** Frames stream to the browser at camera
  rate over the `/stream` WebSocket. The VLM is never in that path — so a slow
  (multi-second) query never stalls the live feed.
- **`POST /ask`** takes `{"question": "..."}`, reads the most recent frame, runs a
  single VLM query, and returns the answer as a streamed `text/plain` response
  (token-by-token typing effect in the UI).
- **Default model:** [`vikhyatk/moondream2`](https://huggingface.co/vikhyatk/moondream2)
  (~1.9B, ~4 GB fp16) — a small VQA model that fits an **NVIDIA Jetson Orin Nano
  (8 GB)** and answers in ~2–5 s. Weights are **baked into the image at build
  time** (`HF_HUB_OFFLINE=1`), so the container needs no network on first run.
- **GPU vs CPU** is chosen from the `WENDY_HAS_GPU` / `WENDY_GPU_VENDOR` build args
  the wendy CLI injects after probing the device (same contract as
  `camera-feed-yolo`), falling back to `torch.cuda.is_available()`.

## Run it

```sh
wendy init --template camera-feed-vlm --language python
cd <app-id>
wendy run
```

On a Jetson the CLI selects the `dustynv/pytorch` CUDA base; on other devices it
uses the generic CPU base. First build is slow (multi-GB base + baked weights).
When ready, the `postStart` hook opens `http://<device>:3006`.

Ask things like *"What do you see?"*, *"How many people are there?"*, or
*"What color is the object on the left?"* — answers track the live scene.

## Files

```
camera-feed-vlm/
├── app.py            # FastAPI: camera streaming (/stream) + VLM chat (/ask)
├── index.html        # Single-page UI: live video + streaming chat panel
├── Dockerfile        # Multi-stage: Jetson CUDA vs generic CPU; bakes the VLM
├── requirements.txt  # CPU/generic deps (Jetson installs on top of dustynv base)
├── template.json     # Wendy template variables (APP_ID, PORT)
├── wendy.json        # App config: gpu + camera + host-network entitlements
└── assets/           # Wendy logo
```

### Endpoints

| Method | Path        | Purpose                                              |
|--------|-------------|------------------------------------------------------|
| GET    | `/`         | The single-page UI                                   |
| WS     | `/stream`   | Binary MJPEG frames; accepts `{"switch_camera": id}` |
| POST   | `/ask`      | `{"question": "..."}` → streamed answer (text/plain) |
| GET    | `/cameras`  | Enumerated capture devices                           |
| GET    | `/debug`    | Mode, model, CUDA flag, capture backend, clients     |
| GET    | `/logs`     | Recent in-memory log lines                           |

## Configuration

Build args (override with `--build-arg`):

- `VLM_MODEL` (default `vikhyatk/moondream2`) and `VLM_REVISION` (default
  `2025-06-21`) — the baked model.

Runtime env:

- `VLM_MAX_TOKENS` — answer length cap.
- `CAMERA_BACKEND=opencv|gstreamer|auto` — force a capture backend.

## Extending

- **Multi-turn chat.** This template answers each question independently on the
  current frame (robust to a moving robot's changing scene). For a conversation
  that remembers prior turns, keep a per-connection history and prepend it to the
  prompt, or switch to a chat-template VLM.
- **A different / stronger model.** Build with
  `--build-arg VLM_MODEL=Qwen/Qwen2.5-VL-3B-Instruct` (needs more VRAM and a
  `qwen-vl-utils` + chat-template inference path in `app.py`'s `/ask` handler).
