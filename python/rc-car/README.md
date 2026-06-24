# rc-car

Remote-control a **Yahboom ROSMASTER R2** (Ackerman-steering robot car on a
Jetson) from a browser, built as a **multi-service app group**: three
single-responsibility containers defined by one native `wendy.json` `services`
map. Modeled on the Go2 `go2-rc` template.

```
rc-car/
├── wendy.json     ← native multi-service app group (3 services)
├── motion/        ← FastAPI motion API (:3201) over the Yahboom STM32 board
├── camera/        ← UVC camera -> MJPEG (:8000)
└── rc/            ← teleop web UI (:RC_PORT) — drive + watch the feed
```

## Architecture

```
                         your browser
                              │  http://<device>:{{.RC_PORT}}
                              ▼
   ┌──────────────────────────────────────────────┐
   │  rc        teleop web UI (FastAPI + static)    │
   │  ─ proxies /api/drive,/stop → 127.0.0.1:3201   │
   │  ─ proxies /api/camera      → 127.0.0.1:8000   │
   └───────────────┬───────────────────┬────────────┘
                   │                   │
        127.0.0.1:3201        127.0.0.1:8000/stream/color
                   ▼                   ▼
   ┌───────────────────────┐  ┌────────────────────────┐
   │  motion  Rosmaster_Lib│  │  camera  OpenCV/V4L2    │
   │  /dev/ttyUSB0 → STM32 │  │  /dev/video0 (UVC)      │
   └───────────┬───────────┘  └────────────────────────┘
               │ serial
               ▼
        Yahboom STM32 driver board → motors + steering servo
```

All three services use `network: host`, so they reach each other on localhost.
`motion` additionally declares the **`serial`** entitlement for `/dev/ttyUSB0`
(the STM32 board), and `camera` declares **`camera`** for `/dev/video0`.

## Services

| Service  | Port | Role | Entitlements |
| -------- | ---- | ---- | ------------ |
| `motion` | 3201 | FastAPI control plane — `POST /drive {throttle,steer}`, `/stop`, `/health`. Wraps Yahboom's `Rosmaster_Lib` (vendored) and owns the serial link. Throttle/steer are clamped and a watchdog stops the car ~0.6 s after the last command. | `network`, `serial` (ttyUSB0) |
| `camera` | 8000 | Captures the generic UVC camera with OpenCV/V4L2 and serves `multipart/x-mixed-replace` MJPEG at `/stream/color`. | `network`, `camera` |
| `rc`     | `{{.RC_PORT}}` | Teleop web UI. Drive with on-screen buttons or W/A/S/D / arrows; proxies motion + camera so the browser uses one origin. Starts after `motion` and `camera`. | `network` |

## Hardware

Designed for the **Yahboom ROSMASTER R2** (Ackerman chassis) on an NVIDIA Jetson:
- The STM32 driver board is a USB-serial (CH340) device at `/dev/ttyUSB0`. Controlled via `Rosmaster_Lib` (`set_car_motion`, `set_akm_steering_angle`); the library is vendored under `motion/` because it is not published on PyPI.
- The camera is a generic **UVC** device at `/dev/video0` — plain V4L2/OpenCV (it is *not* an Intel RealSense).

> **Requires** an agent/CLI new enough to support the `serial` entitlement
> (WendyOS PR #1044 / WDY-1550). Older builds reject `{ "type": "serial" }`.

## Configure & deploy

```sh
wendy init --template rc-car
cd <app-id>
wendy run --device <car-hostname>
```

Then open `http://<car-hostname>:{{.RC_PORT}}` and drive.

> **Safety:** `motion` clamps speed/steering and runs a watchdog that stops the
> car shortly after commands stop arriving. Tune `MAX_SPEED` / `MAX_STEER_DEG`
> via the `motion` service env. Keep a clear area and be ready to hit **STOP**
> (or the spacebar).
