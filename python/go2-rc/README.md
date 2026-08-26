# {{.APP_ID}}

A browser remote control for the Unitree Go2 EDU. It combines a Unitree motion
API, the robot's WebRTC camera, and a teleoperation UI in one app group.

## Requirements

- A Unitree Go2 EDU with a WendyOS computer connected to the robot's
  `192.168.123.0/24` network
- The interface name and Go2 controller IP used by that network
- A clear test area and immediate access to the robot's hardware stop
- Network access during the first build for Python and Unitree SDK dependencies

The Go2 camera permits one WebRTC client. Disconnect the Unitree mobile app or
other camera client before using this application.

## Run and verify

Start with the robot stationary and the area clear:

```sh
wendy run
```

Open `http://<go2-hostname>:{{.RC_PORT}}`. Confirm the camera and status
information load. Send a small joystick or keyboard input, release it, then
press **STOP** and confirm the robot stops.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `APP_ID` | required | App-group identifier |
| `RC_PORT` | `3500` | Browser UI port |
| `GO2_IP` | `192.168.123.161` | Robot controller used for WebRTC camera access |
| `NETWORK_INTERFACE` | `eth0` | Interface on the WendyOS host carrying the robot network |

## How it works

```text
┌─────────┐ HTTP ┌──────────┐ motion API ┌─────────────┐ DDS  ┌─────┐
│ browser │ ◀──▶ │ rc proxy │ ─────────▶ │ motion      │ ───▶ │ Go2 │
└─────────┘      └────┬─────┘   :3201    │ SportClient │      └─────┘
                      │                  └─────────────┘
                      │ /api/camera
                      ▼
                  ┌────────┐ WebRTC ┌────────────┐
                  │ camera │ ◀───── │ Go2 camera │
                  │ :8000  │        └────────────┘
                  └────────┘
```

| Service | Port | Role |
|---|---:|---|
| `motion` | `3201` | Unitree `SportClient` API, DDS state, velocity limits, skills, and watchdog |
| `camera` | `8000` | Go2 WebRTC client re-served as MJPEG at `/stream/color` |
| `rc` | `{{.RC_PORT}}` | Browser UI and proxy for motion and camera APIs |

All services use host networking so they can communicate over localhost and
bind directly to the robot interface. The RC service starts after motion and
camera.

## Extend it

- Add skills or control endpoints in `motion/main.py` and
  `motion/go2_controller.py`.
- Extend camera or audio handling in `camera/`.
- Add proxy routes in `rc/main.py` and controls in `rc/static/index.html`.
- Add a service directory and `wendy.json` entry for an independent robot
  function.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 200
wendy device logs {{.APP_ID}} --service motion --tail 150
wendy device apps stop {{.APP_ID}}
```

If DDS does not connect, verify `NETWORK_INTERFACE` is on the robot network. If
the camera fails while motion works, disconnect other Go2 WebRTC clients and
check `GO2_IP`. Filter logs by service to separate connection failures.

## Safety

- Keep people and obstacles outside the robot's movement area.
- Keep the physical remote and hardware stop available.
- The motion service limits commands to ±0.6 m/s forward, ±0.4 m/s lateral, and
  ±1.0 rad/s yaw.
- A one-second watchdog calls `StopMove` when velocity updates stop. Treat it as
  a backup, not a replacement for the hardware stop.
