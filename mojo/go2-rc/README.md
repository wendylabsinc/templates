# {{.APP_ID}}

A Wendy-branded browser controller for the Unitree Go2 EDU. The control surface
is compiled with Mojo 1.0 and initializes MAX 26.5 on the target accelerator.
The motion service uses the public
[`unitree-mojo`](https://github.com/wendylabsinc/unitree-mojo) v0.2.0 bindings
over Unitree SDK2 for motion, state, and lidar perception. Python is limited to
the current aiortc media sidecar while the native GStreamer bridge is built.

## Requirements

- A Unitree Go2 EDU with a WendyOS computer connected to the robot's
  `192.168.123.0/24` network
- The interface name and Go2 controller IP used by that network
- A clear test area and immediate access to the robot's hardware stop
- Network access during the first build for Modular, Python, and Unitree SDK
  dependencies

The Go2 camera permits one WebRTC client. Disconnect the Unitree mobile app or
other camera client before using this application.

## Run and verify

Start with the robot stationary and the area clear:

```sh
wendy run
```

Open `http://<go2-hostname>:{{.RC_PORT}}`. Confirm that the status bar reports
`MAX 26.5.0: gpu` on a supported Jetson build (or `cpu` on a generic build),
then verify the camera and motion indicators. Send a small joystick or keyboard
input, release it, and press **STOP** to confirm the robot stops.

## Architecture

| Service | Runtime | Port | Role |
|---|---|---:|---|
| `motion` | Mojo 1.0 + Unitree SDK2 | `3201` | Native DDS state and lidar, motion commands, skills, limits, and watchdog |
| `camera` | Python + aiortc | `8000` | Narrow Go2 WebRTC video and talk-audio sidecar |
| `rc` | Mojo 1.0 + MAX 26.5 | `{{.RC_PORT}}` | Branded UI, health/routing, perception transport, and MAX capability report |

The three services use host networking. The browser-facing Mojo service serves
the UI; the UI talks to the motion and camera ports on the same WendyOS host.
The MAX integration intentionally probes the actual compiled accelerator and
reports it in the UI. WebRTC media remains the only Python runtime component.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `APP_ID` | required | App-group identifier |
| `RC_PORT` | `3500` | Browser UI port |
| `GO2_IP` | `192.168.123.161` | Robot controller used for WebRTC camera access |
| `NETWORK_INTERFACE` | `eth0` | Interface carrying the robot network |

## Safety

- Keep people and obstacles outside the robot's movement area.
- Keep the physical remote and hardware stop available.
- Motion commands remain limited to ±0.6 m/s forward, ±0.4 m/s lateral, and
  ±1.0 rad/s yaw.
- A one-second watchdog calls `StopMove` when velocity updates stop. Treat it as
  a backup, not a replacement for the hardware stop.
