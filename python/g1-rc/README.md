# g1-rc — browser remote control for the Unitree G1

Drive a Unitree G1 humanoid from any browser: live camera view full-bleed
behind a touch joystick, posture and gesture buttons, arm presets, and a
big STOP. One `wendy run` deploys all three services to the G1's onboard
computer.

```
┌─────────────┐   HTTP :{{.RC_PORT}}   ┌────────┐  :3201  ┌─────────┐  DDS   ┌───────┐
│   browser   │ ─────────────────────▶ │   rc   │ ──────▶ │ motion  │ ─────▶ │  G1   │
│ (phone/mac) │                        │ (proxy)│  :8000  ├─────────┤        └───────┘
└─────────────┘                        └────────┘ ──────▶ │ camera  │ ◀─ V4L2 (D435i)
                                                          └─────────┘
```

## Services

| Service | Port | What it does |
|---|---|---|
| `motion` | 3201 | FastAPI wrapper around `unitree_sdk2_python`'s G1 `LocoClient`: velocity, posture (balance/stand/squat/sit/damp), gestures (wave/shake), and low-level arm presets over `rt/arm_sdk`. Velocity capped at 0.6/0.4/1.0 (vx/vy/vyaw) with a 1 s watchdog. |
| `camera` | 8000 | OpenCV capture → MJPEG at `/stream/color`. Works with V4L2 indices/paths, RTSP/HTTP URLs, or a GStreamer pipeline. |
| `rc` | `{{.RC_PORT}}` | Serves the teleop UI and proxies to the two services above; degrades cleanly if either is down. |

## Deploy

```bash
wendy run --device <g1-hostname>
```

Then open `http://<g1-hostname>:{{.RC_PORT}}`. (The postStart hook tries to
open it for you.)

## Driving

Locomotion uses the G1's **native stand-up FSM** — the sequence verified on
real hardware (`Start()`/`BalanceStand()` do not work on the firmware this
template targets):

```
stand  = StopMove → "ai" mode → DAMP (FSM 1) → LOCK STAND (FSM 4)
ready-to-walk = LOCK STAND (4) → RUNNING (FSM 801)
```

With the robot suspended or well clear of obstacles:

1. `🧍 stand` → the robot stands (from an unexpected FSM state the UI asks
   you to confirm crane support first, because the path passes through DAMP)
2. `🚶 ready-to-walk` → enters the RUNNING policy; the fsm pill turns green
3. Drive with the joystick / WASD (Q/E turn, shift to run, space = STOP).
   Velocity commands are ignored (with an explicit message) outside RUNNING.

`💤 damp` is the soft-stop: joints go compliant — the robot collapses if
unsupported, so the UI always asks first. Use it whenever you're done or
unsure.

## Safety

- **🛑 E-STOP latches**: it halts walking, fades the arms back to the
  balance controller, and every motion endpoint returns 409 until you
  press `clear e-stop`. It is deliberately *soft* (no torque cut — that
  would drop the robot); the wireless remote's E-stop remains the
  primary hardware stop.
- Velocity commands stop automatically 1 s after the last input
  (watchdog on the motion service).
- Arm presets command only arm joints (15–28); legs and waist stay with
  the robot's balance controller, blended in and out with a weight ramp.
- First sessions: keep the robot on its gantry/hoist.

## Known limitations (v1)

- `unitree_sdk2_python` installs from `master` (the pinned SHA used by
  go2-rc predates the G1 modules). Pin a SHA in `motion/Dockerfile` once
  your build is verified.
- Arm-preset joint indices assume the 29-DOF G1 with 7-DOF arms; verify
  against `/state` on your firmware. If arm init fails, presets return
  errors and the rest of the app keeps working.
- Dex3 hands, audio, and lidar are not wired up.
- `CAMERA_SOURCE` varies by unit: the RealSense D435i colour node is
  typically `4`, but firmware/year differ. If the view is black or shows
  a depth/IR stream, try other indices.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `RC_PORT` | 3500 | Teleop UI port |
| `NETWORK_INTERFACE` | `eth0` | NIC with the robot's `192.168.123.0/24` address (DDS binds here) |
| `CAMERA_SOURCE` | `4` | V4L2 index, `/dev/videoN`, `rtsp://`/`http://` URL, or GStreamer pipeline ending in `appsink` |
