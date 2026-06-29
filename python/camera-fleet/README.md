# camera-fleet

A **multi-device camera fleet** in one template: a camera streamer deployed to *every*
device in a named group (the **edge** tier) plus a single camera-wall dashboard that
**auto-discovers** all of them (the **central** tier).

> [!IMPORTANT]
> This template is a **forward-looking draft**. It uses the fleet "central + edge"
> primitives proposed in **[WDY-1755](https://linear.app/wendylabsinc/issue/WDY-1755)**
> — a placement/topology manifest + cross-device service discovery — which are **not in
> `wendy` yet**. The point of this PR is to pin down the *target developer experience* so
> the implementation can be built against a concrete example. It will not run end-to-end
> until WDY-1755 lands. (Each component on its own — the `camera/` MJPEG app and the
> `dashboard/` viewer — is real and runnable today; only the cross-device wiring is new.)

## The topology

```
        ┌─────────── group: "cameras" ───────────┐
        │  device 1     device 2     …  device N  │      edge tier
        │  [camera]     [camera]        [camera]   │   (one app, replicated)
        └─────┬────────────┬───────────────┬──────┘
              │            │               │
              └────── auto-discovery ──────┘
                           │
                      [dashboard]                          central tier
              (runs once — on an edge device,              (one aggregator)
               a separate hub/laptop, or the cloud)
```

- **`camera/`** — edge component. USB/UVC webcam → HTTP MJPEG (`/stream`, `/stream/color`,
  `/health`). Deployed to every device in the `CAMERA_GROUP`.
- **`dashboard/`** — central component. A camera wall that renders one tile per discovered
  camera and a large selected view. It has **no camera URLs or ports hardcoded** — it is
  fully driven by what the platform discovers.

## How the wiring works (the WDY-1755 contract)

`wendy.json` declares two `components` with **placement**:

```jsonc
"components": {
  "camera":    { "context": "camera",    "target": { "group": "cameras" }, "expose": { "port": 8000, "path": "/stream" } },
  "dashboard": { "context": "dashboard", "target": { "central": true },    "discovers": [ { "component": "camera", "as": "WENDY_FLEET_PEERS" } ] }
}
```

When deployed, the platform:

1. **Fans out** the `camera` component to every device in the `cameras` group, and runs the
   `dashboard` component **once** (`central: true`). The central tier may land on one of the
   edge devices, a separate device (a hub or your laptop), or Wendy Cloud.
2. **Discovers** the live `camera` endpoints (members advertise; membership is dynamic —
   hot-plug / reboot / new device all reflected without a redeploy).
3. **Auto-wires reachability** so the central tier gets a *reachable* `url` regardless of where
   it runs — LAN-direct when co-located, an auto-provisioned cloud tunnel when remote. Treat
   `url` as **opaque**: it is a base origin (`scheme://host:port`) you append your own paths to
   (`/stream`, `/health`); don't assume it is the camera's real host/port.
4. **Injects** the resolved peers into the dashboard two ways:
   - `WENDY_FLEET_PEERS` — a JSON snapshot at start (inline or a file path):
     ```json
     [{ "name": "device-1", "url": "http://…", "group": "cameras", "status": "ready" }]
     ```
   - `WENDY_DISCOVERY_URL` — a local discovery API the app polls for **live** membership.
     **Auto-injected** whenever a component declares `discovers` (not something you put in the
     manifest).
5. Secures tier-to-tier traffic with the **existing WendyOS mTLS** (cert/enrollment, `+1`
   port offset). The mTLS boundary is agent↔agent: it applies to the **remote hop** (when the
   central tier reaches an edge over a cloud tunnel). When central and edge are co-located on
   the LAN, `url` is the edge's plain host port in the same trust domain — no app-level mTLS.

`dashboard/serve.py` consumes exactly that contract; `dashboard/index.html` renders purely
from `GET /api/peers`. That's the whole "after" story: **the dashboard self-populates from
the fleet — adding a camera = a tile appears.**

## Usage (intended)

```sh
# define the device group once (proposed UX — see WDY-1755 open questions)
wendy fleet group create cameras --device cam-01 --device cam-02 …

# render + deploy the whole fleet from one template
wendy init --template camera-fleet --var APP_ID=sh.wendy.examples.camerafleet --var CAMERA_GROUP=cameras
wendy run        # fans `camera` out to the group, starts `dashboard` once
```

Then open the dashboard at `http://<central>:9000` — every camera in the group shows up on
its own.

## Variables

| Variable | Default | What it is |
|---|---|---|
| `APP_ID` | – | Reverse-DNS app id |
| `CAMERA_GROUP` | `cameras` | Named device group the edge component targets / the dashboard discovers |
| `CAMERA_PORT` | `8000` | Per-camera MJPEG port |
| `DASHBOARD_PORT` | `9000` | Central dashboard port |
| `VIDEO_DEVICE` | `/dev/video0` | V4L2 device on each edge device |
| `WIDTH` / `HEIGHT` / `FPS` | `1280` / `720` / `30` | Capture settings |
| `JPEG_QUALITY` | `80` | MJPEG quality (1–100) |

## Today, without WDY-1755

You can still run the pieces manually (this is the setup the template replaces):
deploy `camera/` to each device with `wendy run`, then run the dashboard host-side with
`python3 dashboard/serve.py` and feed it peers via `WENDY_FLEET_PEERS`:

```sh
WENDY_FLEET_PEERS='[{"name":"cam-01","url":"http://localhost:8088","group":"cameras","status":"ready"}]' \
  python3 dashboard/serve.py
```
