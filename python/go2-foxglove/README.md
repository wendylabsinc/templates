# {{.APP_ID}}

A Unitree Go2 data bridge for Foxglove. It converts native Go2 DDS messages into
Foxglove point-cloud, pose, transform, image, and JSON channels and serves them
through one WebSocket connection with a prepared layout.

## When to use this template

`wendy device foxglove serve` is the shorter path for exposing a normal ROS 2
graph through an authenticated tunnel. Use this project when you need its
Go2-specific message conversion, WebRTC camera injection, direct LAN WebSocket,
prepared layout, or source code to extend.

## Requirements

- A Unitree Go2 EDU and a WendyOS computer on its `192.168.123.0/24` network
- The WendyOS computer's own address on that network for DDS binding
- Foxglove Studio or the Foxglove web app on a computer that can reach the
  WendyOS device
- Network access during the first build for Python, CycloneDDS, Unitree, and
  Foxglove dependencies

The Go2 camera permits one WebRTC client. Disconnect the Unitree mobile app or
another camera client before expecting `/go2/camera`.

## Run and verify

```sh
wendy run
```

In Foxglove, open a **Foxglove WebSocket** connection to:

```text
ws://<go2-hostname>:{{.FOXGLOVE_PORT}}
```

Import `foxglove-layout.json` to configure the 3D, camera, state, and UWB
panels. The bridge publishes:

| Channel | Source |
|---|---|
| `/go2/points` | `rt/utlidar/cloud_deskewed` by default |
| `/go2/pose` and `/tf` | `rt/sportmodestate` |
| `/go2/state` | Low-state and sport-mode values as JSON |
| `/go2/uwb` | `rt/uwbstate` as JSON |
| `/go2/camera` | Go2 WebRTC frames forwarded by the camera service |

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `APP_ID` | required | App-group identifier |
| `FOXGLOVE_PORT` | `8765` | Public Foxglove WebSocket port |
| `GO2_IP` | `192.168.123.161` | Robot controller address for WebRTC |
| `GO2_DDS_ADDRESS` | `192.168.123.18` | This WendyOS host's address on the robot network |

`GO2_DDS_ADDRESS` is not the robot controller address. Binding to the host
address prevents a multi-homed device from advertising the wrong DDS subnet.

## How it works

```text
┌──────────────────┐  DDS   ┌──────────────────────────┐  WS  ┌──────────┐
│ Go2 state/LiDAR  │ ─────▶ │ bridge                   │ ───▶ │ Foxglove │
└──────────────────┘        │ channels + camera ingest │      └──────────┘
                            └────────────┴─────────────┘
                                         ▲
                                         │ JPEG HTTP :8766
┌──────────────────┐ WebRTC ┌────────────┴─────────────┐
│ Go2 front camera │ ─────▶ │ camera                   │
└──────────────────┘        └──────────────────────────┘
```

- `bridge/app.py` reads Unitree DDS topics, converts schemas, and owns the
  Foxglove server on `{{.FOXGLOVE_PORT}}`.
- `camera/app.py` receives WebRTC video and posts JPEG frames to the bridge's
  local ingest port `8766`.
- `wendy.json` defines both host-networked services and starts camera after the
  bridge.

The bridge remains useful when the camera service cannot connect; camera
failure does not remove the DDS channels.

## Extend it

- Add a DDS reader and Foxglove channel in `bridge/app.py`.
- Add or change Unitree message conversion in `bridge/pointcloud2.py` or the
  state callbacks.
- Change camera rate or JPEG quality in `camera/app.py`.
- Update `foxglove-layout.json` after adding or renaming channels.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 200
wendy device logs {{.APP_ID}} --service bridge --tail 150
wendy device apps stop {{.APP_ID}}
```

An empty DDS view usually means `GO2_DDS_ADDRESS` is not an address assigned to
the WendyOS host or the expected topics are absent. A missing camera channel
usually means the WebRTC slot is occupied or `GO2_IP` is wrong. In Foxglove, use
`base_link` as the display frame for the supplied layout.
