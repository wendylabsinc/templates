# go2-map-zones

Turn an occupancy map into **labelled, navigable zones** (rooms / coherent open
regions) from a browser. Upload a map, tune three parameters, and get back the
segmented zones as `zones.yaml` plus a rendered visualization.

This is a **pure offline map utility** — it does **not** talk to a Go2 (or any
robot). It pairs with whatever SLAM / mapping stack produced the map: run your
mapping run, save the map, drop it here to carve it into zones an inspection
sweep can visit one at a time.

```
go2-map-zones/
├── wendy.json         ← single-service app (network host)
└── zones/             ← FastAPI service (:PORT)
    ├── segmenter.py   ← the ROS-free watershed segmentation core
    ├── main.py        ← upload + segment + serve results
    └── static/        ← single-page UI
```

## What it does

The segmenter runs a three-stage pipeline, entirely standalone (pure NumPy +
OpenCV, no ROS or live robot):

1. **Obstacle-island fill.** Free-standing props (pallets, racks, chairs,
   boxes) show up as small *occupied* blobs that aren't part of the building's
   wall skeleton. Left alone, each one punctures the free space and splits one
   room into several spurious pieces. Every small free-standing occupied island
   (footprint `< island_max_m2` and not spanning a wall-length run) is
   reclassified as free before segmenting. The single largest connected wall
   structure is never touched.
2. **Distance transform → watershed.** A distance transform of the cleaned free
   space is thresholded at `core_dist_m` to find *room cores* (wide interiors;
   narrow corridors and doorways fall below the threshold). Each core seeds a
   watershed flood that spreads across free space and meets its neighbours at
   walls and mid-doorway — yielding one label per region.
3. **Zone extraction.** Each region becomes `{id, center, polygon, area,
   nav_point, label}` in map (world) coordinates.

**`center` vs `nav_point`:** `center` is the geometric centroid — handy for
labelling, but it can land on a prop or a wall. `nav_point` is the *deepest
originally-free point* inside the zone (the pixel furthest from any real
obstacle), so it's always safe to use as a navigation goal. `label` is a
generic quadrant tag (`NW`, `S`, `center`, …) for reports and markers only.

> Zones are never auto-merged. On open-plan layouts there's no robust local rule
> to tell "one room split in two" from "a room opening onto a corridor", so the
> segmenter keeps the safe invariant: **never emit a mixed-room zone.** A room
> split into two halves is only scanned slightly redundantly, never incorrectly.

## Input formats

Upload **either** of:

- **`.npz`** — a NumPy archive with:
  - `grid` — `int16`, shape `H×W`, values `-1` unknown / `0` free / `100` occupied
  - `res` — float, metres per cell
  - `origin_x`, `origin_y` — floats, world coordinates of the grid origin
- **nav2 `.pgm` + `.yaml`** — a standard `map_saver` pair. The `.pgm` pixel
  intensities (free ≈ 254, occupied ≈ 0, unknown ≈ 205) are thresholded back
  into the occupancy convention above using the `occupied_thresh` / `free_thresh`
  from the `.yaml` (nav2 defaults if absent). Drop both files together.

## Configure

This template prompts for:

- **APP_ID** — the app identifier.
- **PORT** (default `3001`) — where you open the web UI.

## Deploy

```sh
wendy init --template go2-map-zones
cd <app-id>
wendy run --device <device>
```

Then open `http://<device>:{{.PORT}}`, drop in a map, and hit **Segment**.

It runs on any WendyOS device (no GPU, no camera, no dog) — it's just CPU-bound
NumPy/OpenCV, so it's happy on a Raspberry Pi or the Go2's onboard Jetson alike.

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET`  | `/` | The single-page UI. |
| `GET`  | `/health` | `{"status": "ok", "output_dir": …}`. |
| `POST` | `/api/segment` | Multipart upload (`.npz`, or `pgm` + `yaml`) + optional `core_dist_m` / `min_area_m2` / `island_max_m2`. Returns `{id, count, zones, viz_url, yaml_url}`. |
| `GET`  | `/api/result/{id}/viz.png` | The rendered segmentation PNG. |
| `GET`  | `/api/result/{id}/yaml` | The `zones.yaml` (JSON) for that run. |

## Where outputs land

Each segmentation is written to `OUTPUT_DIR/<id>/` as `zones.yaml` +
`zones_viz.png`. `OUTPUT_DIR` defaults to `/data/{{.APP_ID}}/segmentations` (a
mounted WendyOS volume, so results survive restarts); if that isn't writable the
service falls back to `/app/out` inside the container.
