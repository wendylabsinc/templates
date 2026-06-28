# go2-rosbag — Go2 topic recorder &amp; inspector

Deploy to a Go2 and it discovers **every DDS topic the robot exposes**, lets you
**inspect** each one (message schema, a live sample, publish rate, pubs/subs),
hands you **ready-to-use snippets**, and **records an mcap rosbag** of all topics
or just the ones you pick. Bags open directly in Foxglove and are standard
`ros2 bag` (mcap) you can `ros2 bag play`.

## What it does

- Runs **ROS 2 Humble + CycloneDDS + the Unitree message packages**, so
  `ros2 bag record` has type support for the Go2's native topics
  (`rt/lowstate`, `rt/sportmodestate`, `rt/utlidar/*`, `rt/api/*`, …).
- Auto-binds CycloneDDS to the network interface that reaches the robot
  (multi-homed dog → picks the `192.168.123.x` interface).
- Serves a web UI on **`:7000`** to **understand** topics, not just list them:
  - **Filter** by name/type and browse topics **grouped by namespace**.
  - Click any topic to **inspect** it: full **message schema**
    (`ros2 interface show`), a **live sample** (`ros2 topic echo --once`),
    **publishers/subscribers + QoS** (`ros2 topic info -v`), and an on-demand
    **rate** measurement (`ros2 topic hz`).
  - One-click **copy snippets**: `ros2 topic echo`, `ros2 topic info`, and a
    minimal **rclpy subscriber** stub typed to that topic.
- **Records** all topics or a **selected subset** (tick the checkboxes →
  *Record selected*). Bags go to a **`persist`** volume so they survive
  redeploys/reboots.

## Deploy

```sh
wendy run --service recorder --device <dog> -y --detach
```

Then open **`http://<dog>:7000`**:
- The **Topics** card lists every topic + type (also dumped to `/data/topics.txt`),
  filterable and grouped by namespace. Click a topic to inspect it.
- **Record all** → `ros2 bag record -a -s mcap`; or tick topics and **Record
  selected** → `ros2 bag record <topics…>`. **Stop & save** finalizes the bag.
- Download each bag as **`.mcap`** (Foxglove) or **`.tar.gz`** (`ros2 bag play`).

## API

| route | what |
|-------|------|
| `GET /api/topics` | all topics + types |
| `GET /api/topic?name=/x` | type, message schema, pubs/subs, one-shot sample |
| `GET /api/hz?name=/x` | measured publish rate (~5s sample) |
| `POST /api/record/start` | body `{"topics":[…]}` for a subset, or `{}`/none for all |
| `POST /api/record/stop` | finalize the current bag |
| `GET /api/bags`, `GET /download?bag=…&fmt=mcap\|tar` | list / download bags |

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
