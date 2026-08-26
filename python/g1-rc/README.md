# {{.APP_ID}}

A browser remote control for the Unitree G1. It combines a motion API, a camera
stream, and a teleoperation UI with driving, posture, gesture, arm-preset,
diagnostic, and stop controls.

## Requirements

- A Unitree G1 with the supported locomotion service and 29-DOF arm layout
- A WendyOS computer connected to the G1 `192.168.123.0/24` robot network
- A color camera reachable through V4L2, RTSP/HTTP MJPEG, or GStreamer; the G1
  RealSense D435i color node is commonly `/dev/video4`
- A clear test area, the G1 support gantry or hoist for initial testing, and
  immediate access to the robot's hardware stop
- Network access during the first build for Python and Unitree SDK dependencies

## Run and verify

Start with the robot supported and the area clear:

```sh
wendy run
```

Open `http://<g1-hostname>:{{.RC_PORT}}`. Open **Diagnostics** before enabling
motion. It checks the RC proxy, SDK connection, DDS interface, low-state and
battery topics, locomotion state, camera device, and frame freshness.

The safe initial sequence is **stand**, then **ready-to-walk**, then a small
joystick or keyboard input. Press **STOP** and confirm motion stops before
testing further controls.

```text
stand:
┌──────────┐    ┌───────────┐    ┌──────────┐    ┌────────────────┐
│ StopMove │ ─▶ │ "ai" mode │ ─▶ │ DAMP (1) │ ─▶ │ LOCK STAND (4) │
└──────────┘    └───────────┘    └──────────┘    └────────────────┘

ready-to-walk:
┌────────────────┐    ┌───────────────┐
│ LOCK STAND (4) │ ─▶ │ RUNNING (801) │
└────────────────┘    └───────────────┘
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `APP_ID` | required | App-group identifier |
| `RC_PORT` | `3500` | Browser UI and app readiness port |
| `NETWORK_INTERFACE` | `auto` | Interface carrying a `192.168.123.x` address; set a name only if detection fails |
| `CAMERA_SOURCE` | `4` | V4L2 index/path, RTSP or HTTP MJPEG URL, or GStreamer pipeline ending in `appsink` |

## How it works

```text
┌─────────┐ HTTP ┌──────────┐ HTTP :3201 ┌────────┐ DDS  ┌────┐
│ browser │ ◀──▶ │ rc proxy │ ─────────▶ │ motion │ ───▶ │ G1 │
└─────────┘      └────┬─────┘            └────────┘      └────┘
                      │ HTTP :8000
                      ▼
                  ┌────────┐ V4L2 ┌───────┐
                  │ camera │ ◀─── │ D435i │
                  └────────┘      └───────┘
```

| Service | Port | Role |
|---|---:|---|
| `motion` | `3201` | Unitree SDK/DDS control, locomotion state, gestures, arm presets, limits, watchdog, and diagnostics |
| `camera` | `8000` | OpenCV capture and MJPEG at `/stream/color` |
| `rc` | `{{.RC_PORT}}` | Browser UI and proxy for both services |

All services use host networking. The camera service also receives camera and
USB access. The RC service starts after motion and camera; app readiness waits
for its port.

## Extend it

- Add motion operations and safety checks in `motion/main.py` and
  `motion/g1_controller.py`.
- Add or adjust arm poses and joint limits in `motion/g1_arm.py`.
- Change camera input handling in `camera/main.py`.
- Add controls in `rc/web/index.html` and matching proxy routes in `rc/main.py`.
- Keep the controller tests and static UI tests current when changing motion or
  browser behavior.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 200
wendy device logs {{.APP_ID}} --service motion --tail 200
wendy device apps stop {{.APP_ID}}
```

If motion is unavailable, verify the selected interface has a
`192.168.123.x` address and inspect the first SDK or DDS error. If the camera is
black or shows depth/infrared, select another D435i capture node. The motion API
stays available for diagnostics and returns `503` while hardware initialization
is unhealthy.

## Safety

- Keep the robot supported for first tests and clear people and obstacles from
  its range of motion.
- Use the wireless remote's hardware stop as the primary emergency stop.
- The UI E-stop is a latched soft stop; it stops walking and releases arm
  control, but it does not cut torque.
- While the E-stop is latched, motion-causing endpoints return `409`. Press
  **Clear E-stop** to release the latch. **Stop** and **Damp joints** stay
  available while the latch is set because they make the robot safer.
- Velocity is limited to ±0.6 m/s forward, ±0.4 m/s lateral, and ±1.0 rad/s yaw.
  A one-second watchdog calls `StopMove` if commands stop.
- **Damp** makes joints compliant and can cause an unsupported robot to fall.
- Arm presets assume the 29-DOF G1 joint layout. Verify the layout before using
  them on other hardware or firmware.
