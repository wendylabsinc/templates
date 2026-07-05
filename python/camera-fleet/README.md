# camera-fleet

A **multi-device camera fleet** in one template: a camera streamer deployed to *every*
device matching a tag (the **edge** tier) plus a single camera-wall dashboard that
**auto-populates** from all of them (the **central** tier).

> [!NOTE]
> Cameras and the dashboard reach each other over the **mesh**: each discovered camera is
> handed to the dashboard as a mesh name (`device-<assetID>.cloud.wendy.dev:<port>`) that is
> reachable device-to-device — LAN-direct when they share a network, cloud-relayed otherwise.
> So this works whether you deploy by cloud tag (`wendy fleet run`) or over the LAN
> (`wendy fleet run --lan`). Each app carries a `mesh` network entitlement (see its
> `wendy.json`); the dashboard only needs the mesh to *dial* the cameras — it proxies their
> streams to your browser same-origin, so your laptop never joins the mesh.

## Topology

```
        ┌─────────── tag: "camera-*" ────────────┐
        │  camera-01    camera-02    …  camera-N  │      edge tier
        │  [camera]     [camera]        [camera]  │   (one app, replicated)
        └─────┬────────────┬───────────────┬──────┘
              │            │               │
              └──────── discovery ─────────┘
                           │
                      [dashboard]                          central tier
                (tag "central": a device, or             (one aggregator)
                 run it on your laptop)
```

- **`camera/`** — edge app. USB/UVC webcam → HTTP MJPEG (`/stream`, `/stream/color`,
  `/health`). Its own `camera/wendy.json` defines it (camera + `mesh` network entitlements
  that publish its stream port on the mesh, readiness). Deployed to every device matching the
  camera tag.
- **`dashboard/`** — central app. A camera wall that renders one tile per discovered camera,
  with **no camera URLs hardcoded** — it renders purely from `GET /api/peers`. Its own
  `dashboard/wendy.json` defines it (a `mesh` entitlement gives it egress to dial the cameras).
  It **proxies** each camera stream same-origin — the browser only ever talks to the dashboard,
  which reaches the cameras over the mesh (this also sidesteps Chrome Private Network Access
  blocking a page from loading cross-origin MJPEG).

## How it's wired

`wendy-fleet.json` is a thin topology manifest — it **references each app directory** and the
**device tags** it deploys to. App config lives in each app's own `wendy.json`, not here.

```jsonc
{
  "components": {
    "camera":    { "path": "camera",    "tags": ["camera-*"], "expose": { "port": 8000, "path": "/stream" } },
    "dashboard": { "path": "dashboard", "tags": ["central"],  "discovers": [ { "component": "camera", "as": "FLEET_PEERS" } ] }
  }
}
```

`wendy fleet run --lan` then:

1. **Fans out** each component to the devices whose name (LAN) or cloud tag matches one of its
   `tags` (a glob, e.g. `camera-*`). There is no group/central distinction — `central` is just a
   conventional tag. A component whose tags match no device (like `dashboard` here) is deployed
   to `--central <device>`, or its peer snapshot is printed so you can run it on your laptop.
2. **Resolves** the matched `camera` devices to their mesh names.
3. **Injects** them into the dashboard as `FLEET_PEERS` — a JSON snapshot:
   ```json
   [{ "name": "camera-01", "url": "http://device-42.cloud.wendy.dev:8000", "group": "camera-*", "status": "ready" }]
   ```
   `url` is a **base origin** (`scheme://host:port`) reachable over the mesh; consumers append
   their own paths (`/stream`, `/health`). (`WENDY_DISCOVERY_URL`, a live-membership API, is
   reserved for the mDNS work in WDY-1777; unset today, and `serve.py` degrades to the snapshot.)

> The discover `as` name is injected as a container env var, so it must not use a
> reserved prefix (`WENDY_`, `LD_`, `DYLD_`) — the agent rejects those. Use a plain
> name like `FLEET_PEERS`.

`dashboard/serve.py` consumes exactly that contract and renders from `GET /api/peers` — add a
camera to the fleet, a tile appears.

## Hardware differences

Handle Jetson vs Pi / CUDA / JetPack in a **single Dockerfile** via build args — `wendy`
auto-injects the target device's hardware as `--build-arg`s (`WENDY_DEVICE_TYPE`,
`WENDY_GPU_VENDOR`, `WENDY_JETPACK_MAJOR`, `WENDY_CUDA_VERSION`, …). No per-hardware manifests.

## Usage

```sh
wendy init --template camera-fleet \
  --var APP_ID=sh.wendy.examples.camerafleet --var CAMERA_GROUP='camera-*'
wendy fleet run --lan     # fans `camera` out to matching devices; prints the dashboard peers
```

To run the dashboard on your laptop, export the printed `FLEET_PEERS` and start it:

```sh
cd dashboard && FLEET_PEERS='[…]' DASHBOARD_PORT=9000 python3 serve.py
# open http://localhost:9000
```

Or deploy it to a device: `wendy fleet run --lan --central <device>`.

## Variables

| Variable | Default | What it is |
|---|---|---|
| `APP_ID` | – | Reverse-DNS fleet id (each app deploys as `<APP_ID>.<component>`) |
| `CAMERA_GROUP` | `camera-*` | Device **tag** (glob) the camera app deploys to |
| `CAMERA_PORT` | `8000` | Per-camera MJPEG port |
| `DASHBOARD_PORT` | `9000` | Central dashboard port |
| `VIDEO_DEVICE` | `/dev/video0` | V4L2 device on each edge device |
| `WIDTH` / `HEIGHT` / `FPS` | `1280` / `720` / `30` | Capture settings |
| `JPEG_QUALITY` | `80` | MJPEG quality (1–100) |
