# go2-rc

Remote-control the **Unitree Go2 EDU** from a browser, built as a
**multi-service app group**: three single-responsibility containers defined by
one native `wendy.json` `services` map.

```
go2-rc/
├── wendy.json     ← native multi-service app group (3 services)
├── motion/        ← FastAPI motion API (:3201) — the only process linking the Unitree SDK
├── camera/        ← MJPEG/WebRTC camera stream (:8000) from the dog's onboard camera
└── rc/            ← teleop web UI (:RC_PORT) — drive the dog + watch the feed
```

## Architecture

```
                         your browser
                              │  http://<device>:{{.RC_PORT}}
                              ▼
   ┌──────────────────────────────────────────────┐
   │  rc        teleop web UI (FastAPI + static)    │
   │  ─ proxies /api/motion → 127.0.0.1:3201        │
   │  ─ proxies /api/camera → 127.0.0.1:8000        │
   └───────────────┬───────────────────┬────────────┘
                   │                   │
        127.0.0.1:3201        127.0.0.1:8000
                   ▼                   ▼
   ┌───────────────────────┐  ┌────────────────────────┐
   │  motion  SportClient  │  │  camera  WebRTC/MJPEG   │
   │  drives the dog over  │  │  pulls the dog's camera │
   │  CycloneDDS on eth0   │  │  over WebRTC on eth0    │
   └───────────┬───────────┘  └────────────┬───────────┘
               │                            │
               └──────────► Unitree Go2 EDU ◄┘
                          (192.168.123.0/24)
```

The three services talk to each other over **localhost** and to the physical
dog over the **`192.168.123.0/24`** network — both made possible by the
`network: host` entitlement each service declares. This is why there is no
`shared-ipc`/`shared-network` isolation here: host networking already covers
sibling-to-sibling and container-to-robot traffic, and the camera/motion DDS
stacks must bind directly to the robot NIC (`eth0`).

## Services

| Service  | Port | Role |
| -------- | ---- | ---- |
| `motion` | 3201 | FastAPI control plane — `/velocity`, `/move`, `/stop`, `/sit`, …. The only container linking `unitree_sdk2_python`'s `SportClient`; velocity clamps + watchdog live here. |
| `camera` | 8000 | Pulls the Go2's onboard camera over WebRTC and re-serves it as MJPEG at `/stream/color`. |
| `rc`     | `{{.RC_PORT}}` | Teleop web UI. Serves the control page and proxies motion + camera so the browser only needs one origin. Starts after `motion` and `camera`. |

## Configure

This template prompts for:

- **APP_ID** — the app group identifier.
- **RC_PORT** (default `3500`) — where you open the teleop UI.
- **GO2_IP** (default `192.168.123.161`) — the Go2 main-controller IP the camera connects to.
- **NETWORK_INTERFACE** (default `eth0`) — the NIC on the deploy host that carries the `192.168.123.0/24` address. On the Go2's onboard Jetson this is `eth0`.

## Deploy

```sh
wendy init --template go2-rc
cd <app-id>
wendy run --device <go2-jetson>
```

Then open `http://<go2-jetson>:{{.RC_PORT}}` and drive.

> **Safety:** `motion` enforces velocity caps and a 1-second watchdog next to
> the hardware — if the UI stops sending commands, the dog stops. Keep a clear
> area around the robot and be ready to hit **Stop**.
