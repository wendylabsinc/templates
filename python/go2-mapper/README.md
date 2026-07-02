# go2-mapper

Map your home with the **Go2 EDU's onboard Livox Mid-360**, watch the map
build live in Foxglove, mark waypoints (like your front-door approach pose),
and download a **G1-ready navigation bundle**.

A 3-service Wendy app group (like `go2-rc` and `go2-foxglove`): declared in
`wendy.json`, all services on host networking with CycloneDDS bound to the dog
subnet NIC, sharing one `/data` persist volume.

## What you get per export

| File | Consumed by |
|---|---|
| `map.pcd` | G1 LiDAR localization (FAST-LIO-Localization / ICP). Floor-aligned: z=0 is the floor, gravity is +z. |
| `map.pgm` + `map.yaml` | Nav2 `map_server` on the G1. Sliced at **G1 body height** (default 0.15–1.50 m) — tables and counters are obstacles, under-table space the Go2 could walk through is not free for the humanoid. |
| `waypoints.json` | Your marked poses (e.g. `front_door`) in the same floor-aligned frame — feed straight to Nav2 goals. |
| `map_meta.json` | Provenance: floor-fit transform, slice bounds, grid origin. |
| rosbag (separate, under `data/bags/`) | Insurance. Re-run SLAM offline with tweaked params without re-walking. |

## Workflow

1. `wendy run --device <go2-jetson>` on the Go2's Jetson.
2. Open the **console** on your phone: `http://<go2-ip>:{{.CONSOLE_PORT}}`. Open
   **Foxglove** alongside: connect to `ws://<go2-ip>:{{.FOXGLOVE_PORT}}`, load
   `foxglove_mapping.json`.
3. Tap **Start recording**, then joystick the dog slowly through every space
   the G1 will traverse. Watch coverage in Foxglove; loop back to the start.
4. Stand the dog ~0.5 m in front of the door, facing it → type `front_door`
   → **Mark waypoint**.
5. **Finish & export** → download the zip → move the artifacts to the G1.

## Services

- **go2-slam** — Point-LIO (chosen over FAST-LIO2 for robustness to legged-
  robot shake) consuming `/utlidar/cloud_deskewed` + `/utlidar/imu`. Runs a
  sidecar (`viz_bridge.py`) that publishes a 5 cm / 1 Hz downsampled map for
  Foxglove-over-WiFi, a JSON pose topic for the console, and atomic 30 s
  `map.pcd` snapshots so a crash never loses the walk. Compute: ~1 core on
  Orin NX — no external PC needed.
- **go2-recorder** — `ros2 bag record` behind a 3-endpoint HTTP API.
- **go2-console** — FastAPI + single-page UI. No ROS install: reads the pose
  topic via bare `cyclonedds-python`. Runs the export (pure numpy: RANSAC floor fit with a mount-height
  prior so it can't latch onto a bed, then slice → trinary PGM) and serves
  downloads (per-file or zip).

## Honest caveats (read before first build)

- **Point-LIO input path.** Unitree publishes a *standard PointCloud2*, not
  the livox custom msg. `config/mid360_go2.yaml` uses the generic
  preprocessing path — verify `preprocess.lidar_type` against the enum in
  the Point-LIO release you build, and check whether your firmware's cloud
  carries per-point timestamps (deskewed input tolerates their absence).
  This config's topic names/values are a starting point, not gospel.
- **ARM build time.** The Point-LIO image compiles PCL-heavy code; first
  build on the Jetson takes a while. Build once, keep the image.
- **Frame name.** Point-LIO's map frame is commonly `camera_init`; if your
  build differs, update `foxglove_mapping.json` and `viz_bridge.py`'s
  published frame.
- **Single floor assumed.** The export fits ONE global floor plane. Stairs
  or split levels need per-region handling this template doesn't do.
- **No auth on the console.** It can start recordings and serve your home
  map — keep it on the robot LAN only.

## Using the bundle on the G1

- Localization: load `map.pcd` as the prior map; set the G1's lidar-to-base
  transform (head mount, ~1.3 m) — the map's frame is floor-aligned so no
  height fudge is needed.
- Nav2: point `map_server` at `map.yaml`.
- Door goal: read `waypoints.json` → send `front_door` as the Nav2 goal when
  the doorbell fires. Add a depth-camera fine-alignment step at the door
  before the arm sequence — SLAM localization is good to ~5–10 cm, your
  grasp wants better.

## Map quality scoring

Every export is scored automatically (see `go2-console/quality.py`) and the
verdict shows as a badge in the console: **pass / warn / fail**, with hints
(e.g. "doubled walls → re-walk slower, close the loop"). Metrics: wall
crispness (morphological close+erode detects drift-doubled walls ≥ ~15 cm),
floor-fit inlier ratio, coverage, and point density. Full numbers land in
`map_meta.json` under `quality`.

## No-go zones

Open `/nogo` from the console, tap the map to outline forbidden areas
(stair edges, pet beds, sliding rugs), save. This writes `keepout.pgm` +
`keepout.yaml` into the export — Nav2's keepout costmap filter consumes the
pair natively, so the G1 needs zero custom code to respect your zones. The
files are included in the download zip automatically.
