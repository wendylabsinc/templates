# {{.APP_ID}}

Drive a Unitree G1's arms and Dex3 hands with your own hands, wearing a Meta
Quest 3. Turn on recording and each session is saved on the robot as a
training episode: joint states, the commands sent, and head-camera frames.

This is the teleop code that ran on our G1 at the Avangrid conference in
October 2026, without the extras that were added for that demo.

## Requirements

- A Unitree G1 (29-DOF) with Dex3-1 hands and WendyOS on its computer
- A Meta Quest 3 on the same network as the robot
- **A spotter holding the Unitree remote**, watching the robot. There is no
  software dead-man and the operator's hands are busy, so the remote's
  damping combo is the stop.
- The robot standing in running mode (power on → damping → lock stand →
  running mode). The arms are blended into Unitree's own motion controller,
  so they will not move in damping or lock-stand mode.
- Network access during the first build, for conda-forge, PyPI and GitHub

## Run it

```sh
wendy run
```

The first build takes a while: it compiles CycloneDDS and installs Pinocchio
and PyTorch (CPU-only).

1. **Operator console.** Open `https://<robot>:{{.HTTPS_PORT}}/control.html`
   on a laptop and accept the self-signed certificate.
2. **Headset.** In the Quest browser open `https://<robot>:{{.HTTPS_PORT}}/`,
   accept the same certificate, then tap **Enter VR**. The certificate is
   kept across redeploys, so this is a one-time step.
3. **Arm.** On the console press **1 · Take control** (the arms hold their
   current pose), then **2 · ARM**. The arms now follow your hands.
4. **Stop.** **Release** hands the arms back to the robot. The spotter can
   always press the damping combo.

The first time on a new robot, use **Set home** with the arms posed by hand
where every session should start; it is saved on the robot.

```text
power on → damping → lock stand → running mode
                                       │
             console:  1 · Take control  →  2 · ARM
                                       │
                 move your hands; the arms and fingers follow
```

### In the headset

| Control | Does |
|---|---|
| Your hands or controllers | Move the arms and wrists |
| Trigger | Close that hand |
| Right stick left/right | Turn the waist (locked until you unlock it on the console) |
| Left stick | Walk and turn, only when `WALKING` is `on` and the console allows it |
| Left squeeze | Start or stop a recording, only when `RECORDING` is `on` |
| Hold right stick | Give the robot back (release) |
| Hold Meta button | Recenter the panels |

## Data collection

With `RECORDING=on`, start a session from the console or with the left
squeeze. When a session stops it is checked and sealed: a manifest records a
hash of every file, so a session copied off the robot can be verified.

Sessions live in the `{{.APP_ID}}-sessions` volume. List and download them:

```sh
B=https://<robot>:{{.HTTPS_PORT}}
curl -sk "$B/session/list"
curl -sk "$B/session/get?session=<id>"    -o <id>.jsonl
curl -sk "$B/session/frames?session=<id>" -o <id>.frames.tar
```

Each row of the `.jsonl` has the tracked hands and head, the retargeted
targets, the commands actually sent, and the robot's measured joints. Camera
frames come from the robot's head camera (RealSense colour, `/dev/video4`),
which only runs while recording is on.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `APP_ID` | required | App-group identifier |
| `HTTPS_PORT` | `8443` | Headset page and operator console |
| `HTTP_PORT` | `8280` | Same pages over plain HTTP, for a headset on USB only |
| `WALKING` | `off` | `on` lets the left stick walk the robot |
| `RECORDING` | `off` | `on` adds session recording and the head camera |
| `NETWORK_INTERFACE` | `auto` | Interface with the `192.168.123.x` robot address |
| `DDS_DOMAIN` | `0` | `0` is the real robot, `1` a Unitree simulator |

Arm, smoothing and send-rate settings are the conference ones and live as
`ENV` lines in `teleop/Dockerfile` and `onboard/Dockerfile`.

**No key and no dead-man, as at the conference.** Anyone who can reach the
robot's network can open the console and arm it, so keep it on a private
network. To lock it down, set the same secret in `G1_KEY` (onboard) and
`TELEOP_CONTROL_KEY` (teleop); the console then needs `?key=<secret>` in its
URL. To require a held button, set `G1_DEADMAN` to one (`R2`, `L2`, `A`, ...).

## How it works

```text
┌──────────┐ WebXR over ┌───────────────┐   ws :8090   ┌───────────────┐  DDS  ┌────┐
│ Quest 3  │ ─────────▶ │ teleop        │ ───────────▶ │ onboard       │ ────▶ │ G1 │
│ browser  │   wss      │ retarget + IK │   targets    │ safety states │       └────┘
└──────────┘            │ recording     │ ◀─────────── │ arm + Dex3    │
┌──────────┐   HTTPS    │               │   joints     │ commander     │
│ console  │ ─────────▶ │               │              │               │
└──────────┘            └───────────────┘              └───────────────┘
```

| Service | Port | Role |
|---|---:|---|
| `onboard` | `8090` | The only code that moves the robot. Starts in STANDBY, holds on Take control, follows only while ARMED, enforces joint limits and its own timeouts |
| `teleop` | `{{.HTTP_PORT}}`, `{{.HTTPS_PORT}}` | Serves the headset page and console, retargets hands to arm and finger targets (Pinocchio IK, DexPilot), records sessions |

Both use host networking and run on the robot's computer. The G1 and Dex3
model files come from Unitree's
[xr_teleoperate](https://github.com/unitreerobotics/xr_teleoperate)
(Apache-2.0) at a pinned commit; see `NOTICE.md`.

## Tests

The suite covers retargeting, the safety states, the recording switch and the
web pages, without a robot or headset. It needs Pinocchio with CasADi, which
pip does not provide, so use conda-forge:

```sh
sh fetch-assets.sh
micromamba create -n g1-teleop -c conda-forge python=3.12 pinocchio casadi numpy pyyaml pytest
micromamba run -n g1-teleop pip install aiohttp==3.10.11 dex_retargeting
micromamba run -n g1-teleop python -m pytest tests -q
```

## Known limits

- The joint limits shipped in `onboard/calibration` were measured on one G1.
  They are never wider than Unitree's datasheet, but your robot's may be
  narrower.
- At the conference the `teleop` service ran on a laptop with the Quest on
  USB. Running both services on the robot's computer is how this template
  deploys, and has not yet been timed on hardware.
