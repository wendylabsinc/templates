# go2-rosbag — Go2 topic recorder

Deploy to a Go2 and it discovers **every DDS topic the robot exposes** and lets
you **record an mcap rosbag** of all of them. The bag opens directly in Foxglove
and is also a standard `ros2 bag` (mcap) you can `ros2 bag play`.

## What it does

- Runs **ROS 2 Humble + CycloneDDS + the Unitree message packages**, so
  `ros2 bag record -a` has type support for the Go2's native topics
  (`rt/lowstate`, `rt/sportmodestate`, `rt/utlidar/*`, `rt/api/*`, …).
- Auto-binds CycloneDDS to the network interface that reaches the robot
  (multi-homed dog → picks the `192.168.123.x` interface).
- Serves a small web UI on **`:7000`** to list topics, start/stop recording,
  and download bags. Bags are written to a **`persist`** volume so they survive
  redeploys/reboots.

## Deploy

```sh
wendy run --service recorder --device <dog> -y --detach
```

Then open **`http://<dog>:7000`**:
- The **Topics** card lists every topic + type (also dumped to `/data/topics.txt`).
- **Start recording** → `ros2 bag record -a -s mcap`; **Stop & save** finalizes it.
- Download each bag as **`.mcap`** (Foxglove) or **`.tar.gz`** (`ros2 bag play`).

## Config (env)

| var | default | meaning |
|-----|---------|---------|
| `GO2_IP` | `192.168.123.161` | any address on the robot's DDS net — used only to pick the bind interface |
| `ROS_DOMAIN_ID` | `0` | Go2 default DDS domain |
| `AUTO_RECORD` | `0` | set `1` to start recording immediately on launch |
| `PORT` | `7000` | web UI / API port |

## Notes

- Recording **all** topics includes high-rate ones (lidar, images) — bags grow
  fast. Stop when you have what you need; check live size in the UI.
- Single recorder at a time. Stop the current one before starting another.
