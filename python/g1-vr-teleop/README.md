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

## Data collection: GR00T and LeRobot datasets

Deploy with `RECORDING=on` (and, optionally, a `TASK`), then:

```text
record on the robot ─▶ pull to a laptop ─▶ export ─▶ train
Start session         pull_sessions.py     export_dataset.py   GR00T: gr00t/data/stats.py, then fine-tune
(task instruction)    (sealed files)       groot | lerobot-v3   LeRobot: lerobot-train
```

**1. Record.** On the console, **Start session** asks for the task as an
instruction ("Pick up the cup and place it on the plate"). Leave it empty to
use the `TASK` variable. The left squeeze in the headset starts and stops a
session too. Arm, demonstrate, release; repeat. **Stop session** checks and
seals the file: a manifest records a hash of every file.

Each ARMED stretch of at least 2 s becomes one training episode. Pauses and
holds split episodes, so one session can hold many demonstrations. Each row
has the tracked hands and head, the retargeted targets, the command sent to
the robot and the measured joints. Head-camera frames (RealSense colour,
`/dev/video4`, 640x480 at 15 fps) are kept while ARMED.

**2. Pull** the sealed sessions to a laptop:

```sh
python3 tools/pull_sessions.py --robot <robot>:{{.HTTPS_PORT}} --out sessions/
```

**3. Export** (needs `pip install -r tools/requirements-export.txt` and ffmpeg):

```sh
python3 tools/export_dataset.py sessions/*.manifest.json --output datasets/pick-cup --format both
```

| `--format` | Writes | For |
|---|---|---|
| `groot` (default) | LeRobot v2.1 + `meta/modality.json` + `new_embodiment_config.py` | Isaac-GR00T fine-tuning |
| `lerobot-v3` | LeRobot v3.0, made by LeRobot's own converter (`pip install 'lerobot[dataset]'`) | `lerobot-train`, Hugging Face Hub |
| `both` | `<output>/groot` and `<output>/lerobot-v3` | Both |

Every episode has: `observation.state` and `action` (29 joints, radians:
waist yaw, 7 + 7 arm, 7 + 7 Dex3), `observation.images.ego_view` (h264
mp4), the task instruction, timestamps and `next.done`.
`meta/episodes_stats.jsonl` holds per-episode statistics, and
`wendy-export.json` names every source session by its hash.

The exporter refuses rather than invents. It verifies each seal before
reading, uses only ARMED rows, never fills a missing measurement, and refuses
an episode whose video would repeat too many stale frames. If anything is
refused, nothing is written and every reason is listed.

**4. Train.**
- **GR00T** needs `meta/stats.json`, which it computes itself: `python
  gr00t/data/stats.py --dataset-path datasets/pick-cup/groot --embodiment-tag
  NEW_EMBODIMENT --modality-config-path
  datasets/pick-cup/groot/new_embodiment_config.py`. Then fine-tune with the
  same `--modality-config-path`.
- **LeRobot:** `lerobot-train --dataset.repo_id=local/pick-cup
  --dataset.root=datasets/pick-cup/lerobot-v3 ...`

**Success labels (optional).** The headset has no success/fail buttons. To
train on successes only, write `<sid>.labels.json` beside a pulled session,
then export with `--outcomes success`:

```json
{"schema": "wendy.g1.offline-labels.v1", "session": "<sid>", "episode_sha256": "<from the manifest>",
 "outcomes": [{"type": "outcome", "t": <unix time inside the attempt>, "result": "success"}]}
```

The default exports every episode.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `APP_ID` | required | App-group identifier |
| `HTTPS_PORT` | `8443` | Headset page and operator console |
| `HTTP_PORT` | `8280` | Same pages over plain HTTP, for a headset on USB only |
| `WALKING` | `off` | `on` lets the left stick walk the robot |
| `RECORDING` | `off` | `on` adds session recording and the head camera |
| `TASK` | empty | Instruction recorded with each session when the console's box is left empty |
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
