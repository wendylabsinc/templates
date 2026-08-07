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

## If something is not working

Open **Diagnostics** in the top status bar. The panel checks the RC proxy,
Unitree SDK connection, DDS interface, low-state and battery topics, FSM
readback, camera device, and frame freshness. A failed check includes the
underlying error and a concrete next step. Browser request failures also stay
visible in the panel instead of disappearing silently.

For the complete service traceback, run:

```bash
wendy device logs {{.APP_ID}} --device <g1-hostname> --tail 200
```

To narrow that output to one container, add `--service motion`,
`--service camera`, or `--service rc`. Start with the first error in the
failing service; later connection errors are often just a consequence of it.

Common first-run failures:

| UI check | Usually means | What to check |
|---|---|---|
| `motion · failed` | PC2 cannot reach the G1 robot bus or the Unitree SDK did not load | The robot is fully powered on; `NETWORK_INTERFACE` has a `192.168.123.x` address; the first motion traceback |
| `lowstate_received: false` | DDS started but no G1 state samples arrived | Robot power, the robot-bus interface, and competing DDS configuration |
| `fsm_fresh: false` | Unitree's locomotion service is not answering | Put the G1 in its normal powered-on control mode and inspect the motion events |
| `camera · failed` | The selected V4L2 node is missing, busy, or not the color stream | Stop other camera apps and try the D435i color node, commonly `/dev/video4` |

The motion service deliberately keeps its diagnostics API alive when SDK/DDS
startup fails. Motion endpoints still return `503` and the browser stays
fail-closed until the connection is healthy.

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
   Opening the page does not send a movement command; input stays disabled until
   the FSM pill reports `801 (walk-ready)`.

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
| `NETWORK_INTERFACE` | `auto` | Auto-detects the NIC with the robot's `192.168.123.0/24` address. Set the interface name only if detection fails. |
| `CAMERA_SOURCE` | `4` | V4L2 index, `/dev/videoN`, `rtsp://`/`http://` URL, or GStreamer pipeline ending in `appsink` |
