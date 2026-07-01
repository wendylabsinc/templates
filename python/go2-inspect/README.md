# go2-inspect

Open-vocabulary object **inspection** over the **Unitree Go2 EDU**'s front
camera. Point the dog at a scene, describe what you're looking for in plain
English — *"fire extinguisher, exit sign, spill on the floor"* — and watch a
live annotated stream. Hit **Capture** to save an annotated snapshot, per-object
crops, and a growing report.

Unlike `camera-feed-yolo` (fixed 80-class COCO), detection here is
**open-vocabulary** (YOLOE): the target classes are text prompts you set at
runtime from the web UI, no redeploy needed.

```
go2-inspect/
├── wendy.json          ← single-service app (network host + gpu)
└── inspect/            ← the whole thing
    ├── main.py         ← WebRTC video worker + FastAPI (stream, prompts, capture, report)
    ├── detector.py     ← YOLOE open-vocab detector (runtime-configurable prompts)
    ├── report.py       ← draw boxes + append captures to report.md / report.csv
    ├── static/         ← single-page web UI
    ├── cyclonedds.xml  ← DDS NIC binding (from the proven go2-rc camera stack)
    └── Dockerfile      ← slim base + CycloneDDS from source + baked YOLOE weights
```

## Architecture

```
                         your browser
                              │  http://<device>:{{.INSPECT_PORT}}
                              ▼
   ┌────────────────────────────────────────────────────┐
   │  inspect (FastAPI)                                   │
   │  ├─ WebRTC worker  → dog camera → BGR frames         │
   │  ├─ /stream/raw    → MJPEG (cheap, always up)        │
   │  ├─ /stream/annotated → MJPEG + YOLOE boxes (capped) │
   │  ├─ /api/prompts   → get / set open-vocab targets    │
   │  ├─ /api/capture   → detect + save annotated + crops │
   │  └─ /api/report    → the running report as JSON      │
   └───────────────────────────┬────────────────────────┘
                                │ WebRTC on {{.NETWORK_INTERFACE}}
                                ▼
                         Unitree Go2 EDU
                        (192.168.123.0/24)
```

A single container pulls the dog's camera over **WebRTC** (video track only)
and re-serves it as MJPEG, running **YOLOE** open-vocabulary detection over it.
The `network: host` entitlement lets it bind directly to the robot NIC. The
`gpu` entitlement is opt-in headroom — inference defaults to **CPU torch**, so
it runs anywhere (a Jetson with GPU will simply be faster).

## Configure

This template prompts for:

- **APP_ID** — the app identifier.
- **INSPECT_PORT** (default `3400`) — where you open the inspection UI.
- **GO2_IP** (default `192.168.123.161`) — the Go2 main-controller IP the
  camera connects to over WebRTC.
- **NETWORK_INTERFACE** (default `eth0`) — the NIC on the deploy host that has
  the `192.168.123.0/24` address (on the Go2's onboard Jetson this is `eth0`).

## Render & deploy

```bash
# Render to a temp dir (or use the /render-template slash command)
wendy init --app-id my-inspect --template go2-inspect --language python \
    --var INSPECT_PORT=3400 --var GO2_IP=192.168.123.161

# Deploy to the device (usually the dog's onboard Jetson)
cd my-inspect && wendy run --device <hostname>
```

Then open `http://<device>:{{.INSPECT_PORT}}`.

## Using it

1. **Set prompts** — type your target objects (one per line, or comma-separated)
   and hit *Set prompts*. This re-encodes the YOLOE text embeddings live. Start
   broad (`person`, `box`, `chair`) and refine with descriptive phrases
   (`red fire extinguisher`, `cardboard box on a pallet`).
2. **Watch** — toggle between the **Annotated** stream (boxes drawn) and the
   **Raw** stream (no inference, smoother).
3. **Capture** — snapshots the current frame, runs a full-quality detection,
   and saves an annotated JPEG + one crop per object, appending a row to the
   report.

Captures and the report (`report.md` + `report.csv`) land in `CAPTURE_DIR`,
default `/data/{{.APP_ID}}/captures` (mount a `/data` volume in WendyOS to
persist them across restarts; the service falls back to `/app/captures` if
`/data` isn't writable).

## Performance note (CPU-first)

Inference defaults to CPU, where YOLOE can't keep up with the ~30 fps camera.
That's by design: the **Capture** button is the real workflow (on-demand,
full-resolution, one frame). The **Annotated** stream is a live preview,
throttled to `YOLO_MAX_FPS` (default `2`) and re-using the last boxes between
inferences. For a smoother annotated stream, deploy to a GPU device and lower
the inference `imgsz` (`YOLO_IMGSZ`) — or just use the Raw stream to aim and
Capture to inspect.

## Weights baked at build time

The Dockerfile downloads `yoloe-11s-seg.pt` **and** the MobileCLIP text backend
during the build (the first `get_text_pe()` call), so a deployed container never
needs the network — container DNS may not be up at boot on the dog. Prompt
embeddings are cached on disk keyed by the prompt strings, so repeated prompt
sets are instant.

## Caveats

- **One WebRTC client per dog.** If the Unitree phone app (or another
  WebRTC consumer like `go2-rc`'s camera or `go2-foxglove`) holds the slot,
  this can't connect. Close the app first.
- **Camera AES-key issue.** On Go2 firmware ≥ 1.1.15, the WebRTC LAN handshake
  requires a per-device AES-128 key. If the camera never connects, see
  [`../../go2-rc-camera-aes-key-issue.txt`](../../go2-rc-camera-aes-key-issue.txt)
  for the root cause and how to fetch the key.
- **No SLAM / world coordinates.** There's no onboard map on this path, so
  detections aren't localized on a floorplan — they're annotated on the camera
  frame only.
