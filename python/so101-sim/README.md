# {{.APP_ID}}

A simulated SO-101 follower arm. It runs the arm in the MuJoCo physics engine
on your Wendy device and shows it in 3D in your browser. You can move its six
joints from the page, or from your own code over HTTP.

The arm is virtual only. It has no USB serial port and no servos. The
`so101-calibration` template and other real-arm code that talk to Feetech
servos over serial cannot connect to it.

## Requirements

- A Wendy device with a running Wendy agent, on arm64 or x86_64 Linux
- Wendy CLI access to that device
- Network access during the first build. The build downloads Python packages,
  MuJoCo, Mesa CPU rendering libraries, the SO-101 model, and Three.js.
- A browser with WebGL 2 for the 3D view

No GPU, camera, or USB device is needed. Physics and the snapshot image run on
the CPU. The build checks every downloaded model and viewer file against a
SHA-256 hash in `assets.lock.json` and `viewer-assets.lock.json`.

## Run

```sh
wendy init my-arm-sim --template so101-sim
cd my-arm-sim
wendy run
```

The first build takes a few minutes. After the container starts, Wendy waits up
to 120 seconds for the simulator to listen on port {{.PORT}}. The log then
prints `SO-101 simulator ready; virtual model only; HTTP :{{.PORT}}`.

## Open the viewer

The simulator only answers requests addressed to `localhost` or `127.0.0.1`.
This check comes unchanged from the WendyOS simulator. If you open
`http://<device>:{{.PORT}}` from another computer, you get
`Invalid simulator host` (HTTP 403).

Forward the port to your computer, then open `http://localhost:{{.PORT}}`. For a
device enrolled in Wendy Cloud:

```sh
wendy cloud tunnel {{.PORT}}:{{.PORT}} --device "<cloud device name>"
```

`wendy cloud discover` lists the cloud device names. Keep the tunnel running
while you use the page. Code that runs on the device itself, such as another
app with host networking, can call `http://127.0.0.1:{{.PORT}}` directly.

## Move the arm in the viewer

- **3D view:** drag to orbit, Shift-drag to pan, scroll to zoom. **Reset
  camera** frames the arm again.
- **Joint targets:** one slider per joint, in degrees, limited to that joint's
  range. Release a slider to send its target. The number beside each joint
  name is the joint's current position.
- **Pause** stops physics; the 3D view stays live. **Resume** starts it again.
- **Reset** puts the arm back at home, puts the cube back, and resumes physics.
- **Wave** plays an 8-second wave with the shoulder pan, wrist roll, and
  gripper, then returns home. Moving a slider stops the wave.

The arm moves toward each target at up to 0.8 radians per second (about 46
degrees per second). Home is 0 degrees for every joint, with the gripper at
about 23 degrees. The scene also has a floor and a small blue cube.

## Move the arm from your own code

Every request goes to `http://localhost:{{.PORT}}` through the tunnel, or to
`http://127.0.0.1:{{.PORT}}` on the device. Angles are in degrees. POST bodies
are JSON.

```sh
curl http://localhost:{{.PORT}}/api/status
curl -X POST http://localhost:{{.PORT}}/api/pose \
  -H 'content-type: application/json' \
  -d '{"joints_deg":{"shoulder_pan":20,"gripper":30}}'
```

| Method | Path | Body | What it does |
|---|---|---|---|
| `GET` | `/api/status` | none | Health, mode, and each joint's position, target, and limits |
| `POST` | `/api/pose` | `{"joints_deg":{"<joint>":<degrees>}}` | Sets targets for one or more joints |
| `POST` | `/api/pause` | `{"paused":true}` or `{"paused":false}` | Pauses or resumes physics |
| `POST` | `/api/reset` | `{}` or none | Returns home, resets the cube, and resumes |
| `POST` | `/api/demo` | none | Starts the 8-second wave |
| `POST` | `/api/camera` | `{"azimuth":135,"elevation":-25,"distance":0.8}` | Moves the camera for `/camera.jpg` and `/stream` |
| `GET` | `/camera.jpg` | none | Current 640x480 JPEG, rendered on the CPU about 4 times per second |
| `GET` | `/stream` | none | The same images as an MJPEG stream |
| `GET` | `/health` | none | `{"status":"ok",...}`, or HTTP 503 while the simulator is unhealthy |
| `GET` | `/api/scene`, `/api/scene/state` | none | Geometry and body poses that the 3D viewer draws |

`/api/state`, `/api/health`, and `/frame.jpg` are aliases of `/api/status`,
`/health`, and `/camera.jpg`.

`/api/status` returns `joints` as a list of
`{"name", "position_deg", "target_deg", "min_deg", "max_deg"}`. It also returns
`healthy`, `mode` (`joint-control` or `paused`), `paused`, `demo`, `epoch` (it
goes up on every reset), `simulation_seconds`, and `tip_m`, the gripper
position in metres.

Rules for `/api/pose`:

- The joints are `shoulder_pan`, `shoulder_lift`, `elbow_flex`, `wrist_flex`,
  `wrist_roll`, and `gripper`. Joints you leave out keep their targets.
- Each value must be inside the joint's limits. The limits come from the model:

  | Joint | Minimum | Maximum |
  |---|---:|---:|
  | `shoulder_pan` | about -110 | about 110 |
  | `shoulder_lift` | about -100 | about 100 |
  | `elbow_flex` | -96.8 | 96.8 |
  | `wrist_flex` | about -95 | about 95 |
  | `wrist_roll` | -157.2 | 162.8 |
  | `gripper` | about -10 | about 100 |

  Read the exact limits from `min_deg` and `max_deg` in `/api/status`. A
  rounded value such as `110` can fall just outside a limit and be rejected.
- An unknown joint, a value that is not a finite number, a value out of range,
  or an extra field fails with HTTP 422, and no target changes.
- A pose stops the wave. It does not resume a paused simulation.

`/api/camera` accepts `azimuth` from -360 to 360, `elevation` from -85 to -5,
and `distance` from 0.35 to 1.5 metres. It does not move the 3D view in the
browser.

Every POST body must be 4096 bytes or less (HTTP 413). POSTs fail with HTTP 503
while the simulator is unhealthy. A browser page from another origin cannot
send POSTs (HTTP 403).

## Ports

The viewer and API listen on port {{.PORT}} (default 8891). The Wendy Go2
simulator uses 8890, so both can run on one device. The app uses host
networking, so the port is opened directly on the device.

If port {{.PORT}} is taken, generate the project again with
`--var PORT=8892`. To change it in an existing project, edit the port in
`Dockerfile` (`EXPOSE` and the `uvicorn --port` argument), in `wendy.json`
(`readiness`), and in the startup message in `so101_sim/server.py`.

## How it works

- `so101_sim/server.py` loads `model/scene.xml` and steps MuJoCo physics every
  5 ms on its own thread. A second thread renders the JPEG snapshot with Mesa
  (OSMesa) on the CPU. FastAPI serves the page and the API.
- `so101_sim/index.html`, `viewer.js`, and `lidar-view.js` are the page. The 3D
  view uses Three.js from `so101_sim/vendor/`, which the build downloads. The
  page loads nothing from the internet. The lidar view is turned off for the
  arm.
- `so101_sim/scene.py` exports the model geometry and body poses for the page.
- `model/scene.xml` is the lab scene: a floor, a light, and the cube. It
  includes the official SO-101 follower model, which the build downloads into
  `model/`.
- `tools/fetch_assets.py` downloads the pinned model and Three.js files and
  checks their hashes. The `Dockerfile` runs it.
- `wendy.json` grants host networking and waits up to 120 seconds for port
  {{.PORT}}.

## Develop and test on your computer

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest==8.4.2 httpx==0.28.1
.venv/bin/python tools/fetch_assets.py
PYTHONPATH=. .venv/bin/pytest -q tests
```

The tests use the real model and API without a browser or OpenGL. On Linux,
install `libosmesa6` and set `MUJOCO_GL=osmesa SO101_RENDER_TEST=1` to also test
rendering. `tools/smoke_http.py` checks a running simulator, for example
through the tunnel:

```sh
.venv/bin/python tools/smoke_http.py http://127.0.0.1:{{.PORT}} --vm-name ''
```

It moves, pauses, and resets the arm.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 100
wendy device apps stop {{.APP_ID}}
```

- **`Invalid simulator host` (HTTP 403):** the request was not addressed to
  `localhost` or `127.0.0.1`. Use the tunnel described above.
- **`Simulation unavailable` or HTTP 503:** physics or rendering stopped. Check
  the logs, then run `wendy run` again.
- **Readiness times out:** check that port {{.PORT}} is free, and read the first
  build or startup error in the logs. A slow CPU can need most of the 120
  seconds on the first start.

## Licenses and upstream

This simulator is copied from the WendyOS SO-101 simulator in WendyOS pull
request #2245, commit `d5c064fe1dbe883c9075df4d933ab0c44bb1ba9a`
(`go/simulator/so101`). The SO-101 model comes from TheRobotStudio/SO-ARM100
under the Apache-2.0 license. Three.js and OrbitControls are MIT licensed. See
[UPSTREAM.md](UPSTREAM.md) and [licenses/](licenses/).
