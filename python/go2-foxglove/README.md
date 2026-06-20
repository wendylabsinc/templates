# go2-foxglove

Stream a **Unitree Go2**'s live data into **Foxglove** over a single WebSocket —
LiDAR point cloud, pose + TF, body state (IMU / battery / foot forces) and UWB,
plus the front camera. A lighter cousin of `go2-watchtower` (no vision/mic/audio).

```
Go2 controller ──DDS──┐
  192.168.123.161      │   ┌──────────────────────────────┐
                       ├──▶│ bridge  (DDS → Foxglove WS)    │──▶ ws://<device>:8765 ──▶ Foxglove
  Jetson .123.18  ─────┘   │   /go2/points /go2/pose /tf    │
                           │   /go2/state /go2/uwb          │
        front cam ──WebRTC─▶│ camera ──localhost JPEG──▶ /go2/camera
                           └──────────────────────────────┘
```

**Two containers, one connection.** The `camera` service does the heavy WebRTC
decode in isolation and forwards JPEG frames to the `bridge` over localhost, so the
camera appears on the *same* Foxglove connection — but if WebRTC fails, the 3D/LiDAR
view stays up.

## Deploy

```bash
wendy init --template go2-foxglove --language python --app-id go2viz
cd go2viz
wendy run --device <go2>.local
```

Variables (`wendy init` prompts, or pass `--var`):
- **GO2_IP** — the robot controller IP for the camera (default `192.168.123.161`).
- **GO2_DDS_ADDRESS** — *this device's* IP on the robot LAN (default `192.168.123.18`).
  See **Where does this run?** below.
- **FOXGLOVE_PORT** — the WebSocket port (default `8765`).

## View in Foxglove

1. Open Foxglove (desktop app or <https://app.foxglove.dev>).
2. **Open connection → Foxglove WebSocket** → `ws://<device>:8765`.
3. **Layout → Import from file…** → `foxglove-layout.json` (in this template) to get
   the 3D + camera + plots + UWB panels pre-arranged.

You should see the point cloud under the moving robot, the camera image, battery/IMU
and pose/foot-force plots, and the raw UWB message.

## Where does this run? (matters for GO2_DDS_ADDRESS)

DDS binds to **this machine's** IP on the robot LAN — set `GO2_DDS_ADDRESS` to it:
- **On the Go2's onboard Jetson:** usually `192.168.123.18` (the default).
- **On an external Jetson** bridged to the robot LAN: that machine's `192.168.123.x`.

Binding by **address** (not interface name) is deliberate — the Go2 Orin is
multi-homed (`eth1` carries two subnets), so a name is ambiguous and DDS can
advertise the wrong subnet.

## Notes / caveats (unverified on a live EDU+ — verify on the robot)

- **foxglove-sdk API**: the bridge uses the `foxglove-sdk` channel/schema classes;
  pin the version you validate (`bridge/requirements.txt`).
- **LiDAR**: assumes `rt/utlidar/cloud_deskewed` (override with `LIDAR_TOPIC`). The
  EDU+ ships the **Livox MID-360**; confirm that topic is published on your firmware.
- **Camera**: the Go2 allows **one** WebRTC client — if the Unitree phone app is
  connected, the camera can't connect until it disconnects.
- **arm64**: the Go2's Orin is arm64. Build with `--platform linux/arm64` if building
  the images from an x86 host.
- **Frames**: the 3D panel's *Display frame* is `base_link`; if the cloud or pose
  looks off, switch the display frame in the panel settings.
