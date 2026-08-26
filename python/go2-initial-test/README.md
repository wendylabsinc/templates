# {{.APP_ID}}

A multi-service hardware check for the Unitree Go2 EDU. A browser dashboard
collects independent interface tests into one pass, fail, pending, manual, or
not-applicable view. Motion and sound actions require explicit user input.

## Requirements

- A Unitree Go2 EDU with a WendyOS computer on the robot network
- An ARM64 Jetson target with enough disk and memory for 14 containers
- A GPU base image compatible with the target's JetPack release
- Camera, LiDAR, microphone, speaker, Bluetooth, and network services available
  for the checks you expect to pass
- A stand or clear motion area and immediate access to the robot's hardware stop
- Free host ports `3610` through `3622` plus `{{.UI_PORT}}`

The GPU image and source-built SDK/DDS services make the first build and deploy
large. A failed or unavailable service appears as pending instead of hiding its
tile.

## Run and verify

Start with motion safely constrained:

```sh
wendy run --detach
```

Open `http://<go2-hostname>:{{.UI_PORT}}`. Review automatic checks first. Run
speaker, light, motion, and other manual checks only when the environment is
ready. `GET /api/status` polls `GET /status` on each test service.
`POST /api/run/{key}` forwards `POST /run` to the service that owns the selected
check.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `APP_ID` | required | App-group and storage-volume prefix |
| `UI_PORT` | `3600` | Dashboard port |
| `GO2_IP` | `192.168.123.161` | Robot controller and route target used for DDS bind-address detection |
| `GPU_BASE_IMAGE` | `dustynv/pytorch:2.7-r36.4.0-cu128-24.04` | GPU test image; must match target JetPack |
| `CLOUD_HEALTH_URL` | Wendy Cloud service URL | Endpoint used by the cloud reachability check |

Set `GO2_DDS_ADDRESS` at runtime only when automatic route-based address
selection is wrong. Service-specific settings are documented beside their
defaults in the service source and Dockerfiles.

## How it works

The dashboard fans out to independent test services over localhost and merges
their status records. Each service can fail without hiding the other results.

### Checks and services

| Service | Port | Main checks |
|---|---:|---|
| `gpu` | `3610` | CUDA availability and a small operation |
| `lowstate` | `3611` | IMU, foot force, battery, joints, odometry, remote, and UWB state |
| `camera` | `3612` | One Go2 WebRTC frame |
| `lidar` | `3613` | Point-cloud receipt and point count |
| `mic` / `speaker` | `3614` / `3615` | ALSA capture level and manual DDS playback |
| `motion` | `3616` | Manual movement, posture, gait, and guarded advanced actions |
| `cloud` | `3617` | Internet and configured endpoint reachability |
| `extras` | `3618` | Unsupported or probe-only interfaces such as ultrasonic and gimbal |
| `storage` | `3619` | Persistent write/read and free space |
| `vui` | `3620` | Head light and volume controls |
| `bt` / `btscan` | `3621` / `3622` | Adapter presence and active Bluetooth scan |

`ui` has no service dependencies, so it can report unavailable test services.
All services use host networking. Only the services that need GPU, persistent
storage, or Bluetooth receive those additional entitlements.

## Extend it

- Add a service directory that returns the existing `results` status shape.
- Add the service to `wendy.json`, the `SERVICES` map, and tile definitions in
  `ui/main.py`.
- Keep hardware access isolated to the new service and grant only its required
  entitlements.
- Return actionable detail from expected failures; leave unknown interfaces as
  `na` or `pending` instead of reporting a false pass.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 250
wendy device logs {{.APP_ID}} --service <service> --tail 150
wendy device apps stop {{.APP_ID}}
```

Use `wendy run --service <service>` while iterating on one check. An empty DDS
group usually means the host selected the wrong robot-network address. A camera
failure can mean another WebRTC client owns the Go2 camera. A GPU failure can
mean `GPU_BASE_IMAGE` does not match the installed JetPack release.

## Safety

- Keep the robot supported or in a clear area for every motion check.
- Keep the physical remote and hardware stop available.
- Motion is manual-only and should stop after each test; confirm the stop before
  moving to another tile.
- Advanced acrobatics require both UI confirmation and
  `ENABLE_ACROBATICS=1`. Do not enable them during routine interface checks.
- Do not interpret an unavailable service, occupied port, or exhausted resource
  as a hardware failure until its service log is checked.
