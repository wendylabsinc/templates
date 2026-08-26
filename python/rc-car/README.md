# {{.APP_ID}}

A browser and gamepad remote control for the Yahboom ROSMASTER R2 Ackermann car.
It combines the STM32 motion link, Angstrong Nuwa-HP60C color/depth camera, web
UI, and optional joystick input in one app group.

## Requirements

- A Yahboom ROSMASTER R2 with its STM32 USB-serial controller
- The bundled Angstrong Nuwa-HP60C camera and supported ARM64 camera SDK
- A WendyOS device with the controller on one of `/dev/ttyUSB0` through
  `/dev/ttyUSB2`
- An optional Linux joystick at `/dev/input/js0`, connected by USB or Bluetooth
- A clear test area and immediate access to vehicle power

The camera is not a generic UVC source. The `camera` service uses the vendored
Angstrong SDK and encrypted HP60C configuration.

## Run and verify

Raise the drive wheels or place the car in a clear area for the first run:

```sh
wendy run
```

Open `http://<car-hostname>:{{.RC_PORT}}`. Check `/api/health`, confirm the color
camera view, send a small steering and throttle input, release it, then press
**STOP**. The joystick service is optional; idle joystick input does not take
control from the browser.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `APP_ID` | required | App-group identifier |
| `RC_PORT` | `3500` | Browser UI port |

Important runtime settings are read from the service environment. Some defaults
are set in the Dockerfiles and the rest are fallback values in the service
source:

- Motion: the Dockerfile sets `ROSMASTER_COM=auto` and `MAX_SPEED=0.6`;
  `motion/app.py` supplies `STEER_SERVO=1`, `STEER_CENTER=90`,
  `STEER_RANGE=70`, `STEER_SIGN=1`, and `WATCHDOG_S=0.6`.
- Camera: `ASCAM_CONFIG`, `JPEG_QUALITY=80`, and `CAMERA_FLIP=none`.
- Joystick: the Dockerfile sets `JOYSTICK_DEV=/dev/input/js0`, axis indices, and
  `THROTTLE_SIGN=-1`; `joystick/app.py` supplies `STEER_SIGN=1`,
  `DEADZONE=0.08`, `SEND_HZ=15`, and `ESTOP_BUTTON=1`.

Use `wendy run --env KEY=VALUE` for a temporary runtime override, or edit the
relevant Dockerfile or source fallback for a project default.

## How it works

```text
┌─────────┐ HTTP ┌─────────────┐
│ browser │ ───▶ │ rc UI/proxy │ ────────────┐
└─────────┘      └─────────────┘             │
                                             │ HTTP drive/stop
┌─────────┐ /dev/input/js0 ┌──────────┐      │
│ gamepad │ ─────────────▶ │ joystick │ ─────┤
└─────────┘                └──────────┘      │
                                             ▼
                                         ┌────────┐ serial ┌───────┐
                                         │ motion │ ─────▶ │ STM32 │
                                         │ :3201  │        └───────┘
                                         └────────┘

┌───────┐ USB/SDK ┌────────┐ color/depth MJPEG :8000 ┌─────────┐
│ HP60C │ ──────▶ │ camera │ ──────────────────────▶ │ browser │
└───────┘         └────────┘                         └─────────┘
```

| Service | Port | Role and access |
|---|---:|---|
| `motion` | `3201` | Probes the serial nodes for the ROSMASTER controller and exposes drive, stop, test, and health APIs |
| `camera` | `8000` | Uses the Angstrong SDK and serves `/stream/color`, `/stream/depth`, and `/health` |
| `rc` | `{{.RC_PORT}}` | Browser UI and proxy for drive and stop requests |
| `joystick` | `3600` | Reads a Linux joystick and sends commands to the motion API |

`wendy.json` grants the motion service three serial nodes, grants camera and USB
access to the camera service, and grants input and Bluetooth access to the
joystick service. All services use host networking.

## Extend it

- Change serial probing, limits, steering calibration, or endpoints in
  `motion/app.py`.
- Change camera streams and encoding in `camera/bridge.cpp`.
- Add UI controls in `rc/static/index.html` and proxy routes in `rc/main.py`.
- Change gamepad mapping in `joystick/app.py`.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 200
wendy device logs {{.APP_ID}} --service motion --tail 150
wendy device apps stop {{.APP_ID}}
```

If motion returns `503`, inspect the selected serial node and controller probe.
If camera health reports zero frames, check the HP60C USB connection, config,
and camera-service log. If gamepad axes are reversed, change the joystick sign
settings rather than swapping motion controls.

## Safety

- Raise the wheels or use an open test area during setup.
- Keep vehicle power within reach; software stop is not a power disconnect.
- The motion service clamps throttle and steering and stops after about 0.6
  seconds without a command.
- Increase speed, steering range, or watchdog time only after verifying the
  current limits and the physical stop path.
