# {{.APP_ID}}

A simulated Unitree Go2 robot dog. It runs MuJoCo physics with a trained walking
policy, publishes ROS 2 topics like the real robot, and shows the robot in a 3D
viewer in your browser. Use it to test robot apps without the real Go2.

The robot stands in a small room with an orange box. You can drive it from the
browser, from an HTTP client, or from a ROS 2 app on the same device.

## Requirements

- A Wendy device with the Wendy agent: WendyOS or another Linux, on x86_64 or
  ARM64
- Network access during the first build. It downloads the ROS 2 Humble base
  image, MuJoCo and other Python packages, and the pinned robot model, walking
  policy and Unitree message definitions from GitHub.
- Time and disk space: the first build takes a few minutes, and the image is
  large (about 780 MB compressed)
- CPU and memory: physics, the walking policy and the robot camera all run on
  the CPU. The Wendy simulator VM that normally runs it has 4 CPU cores and
  4 GiB of memory by default; plan for about the same.
- A browser with WebGL 2 for the 3D viewer

## Run

```sh
wendy init my-sim --template go2-sim
cd my-sim
wendy run
```

Wendy waits up to 120 seconds for the simulator to open its port. Then open
`http://<device-hostname>:{{.PORT}}` in a browser. The badge in the top right
shows the robot's mode, for example `standing`.

## Make the robot move

### From the browser

1. Click **Enable controls**.
2. Hold <kbd>W</kbd> or <kbd>S</kbd> to walk forward or back, <kbd>A</kbd> or
   <kbd>D</kbd> to step left or right, and <kbd>Q</kbd> or <kbd>E</kbd> to turn.
   <kbd>Space</kbd> stops. The robot stops when you let go of the keys.
3. Click **Release controls** when you are done.

Driving needs a keyboard; touch screens can only move the view. The page must
have focus, so click the 3D view first if keys do nothing.

**Pause** freezes the world and **Resume** continues it. After **Pause** or
**Reset world**, click **Enable controls** again. If the robot falls over, click
**Reset world**. **Robot camera** shows the robot's front
camera. **Obstacle placement** moves the orange box, and **Sensor faults** turns
the lidar or camera off.

Only one controller can drive at a time: the browser, an HTTP client, or one
ROS 2 app. **Enable controls** fails while another one has control.

### From an HTTP client

The viewer uses a small HTTP API, and you can call it yourself. Take control,
then send a velocity at least every 200 ms, because older commands expire and
the robot stops:

```sh
SIM=http://<device-hostname>:{{.PORT}}

# Take control. The reply contains a token.
TOKEN=$(curl -s -X POST $SIM/api/arm -H 'Content-Type: application/json' -d '{}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')

# Walk forward at 0.3 m/s for about 3 seconds.
for i in $(seq 60); do
  curl -s -X POST $SIM/api/command -H 'Content-Type: application/json' \
    -d "{\"token\":\"$TOKEN\",\"velocity\":[0.3,0,0]}" > /dev/null
  sleep 0.05
done

# Give control back.
curl -s -X POST $SIM/api/release -H 'Content-Type: application/json' \
  -d "{\"token\":\"$TOKEN\"}"
```

`velocity` is `[forward m/s, left m/s, turn left rad/s]`, limited to 0.8, 0.5
and 1.0. `GET $SIM/api/status` returns the mode, position and joint states.
Other endpoints are `/api/stop`, `/api/pause`, `/api/resume` and `/api/reset`.

## Connect your own app

The simulator speaks ROS 2 Humble over CycloneDDS on domain 0. Its CycloneDDS
configuration, [cyclonedds.xml](cyclonedds.xml), uses only the loopback
interface and finds other programs by unicast on 127.0.0.1. So only apps on the
same device, on the host network, with the same loopback configuration, can see
its topics. Computers on your network cannot.

An app that does not need ROS can use the HTTP API above at
`http://127.0.0.1:{{.PORT}}`, as long as it uses host networking.

A ROS 2 app needs these four things:

1. Host networking in its `wendy.json`, and the same `frameworks.ros2` block as
   this project:

   ```json
   "entitlements": [{ "type": "network", "mode": "host" }],
   "frameworks": {
     "ros2": { "distro": "humble", "rmw": "rmw_cyclonedds_cpp", "domainId": 0, "scope": "device" }
   }
   ```

2. A copy of this project's `cyclonedds.xml` in its image, for example at
   `/app/cyclonedds.xml`.
3. These settings in the command that starts the app, for example a start
   script. Setting them as `ENV` in the Dockerfile is not enough: Wendy adds its
   own ROS 2 settings to apps that declare `frameworks.ros2`, and those replace
   the Dockerfile values. The simulator's `entrypoint.sh` does the same.

   ```sh
   export ROS_DOMAIN_ID=0 RMW_IMPLEMENTATION=rmw_cyclonedds_cpp ROS_LOCALHOST_ONLY=0
   export CYCLONEDDS_URI=file:///app/cyclonedds.xml
   ```

   `ROS_LOCALHOST_ONLY=0` matters on Humble: its localhost-only option adds a
   second loopback interface that conflicts with the file.
4. To drive, a `geometry_msgs/msg/Twist` publisher on `/cmd_vel` that sends
   `linear.x`, `linear.y` and `angular.z` 10 to 20 times a second, within the
   limits above. Leave the other fields at zero.

The simulator never gives control to an app by itself. While your app is
publishing, open **App control** in the viewer, choose your app's node in
**Publisher**, and click **Grant control**. After **Pause**, **Reset world** or
**Release control**, restart your app's publisher before you grant control again.

Main topics your app can read:

| Topic | Type | Rate |
|---|---|---|
| `/odom`, `/utlidar/robot_odom` | `nav_msgs/msg/Odometry` | 50 Hz |
| `/imu/data` | `sensor_msgs/msg/Imu` | 200 Hz |
| `/joint_states` | `sensor_msgs/msg/JointState` | 50 Hz |
| `/scan` | `sensor_msgs/msg/LaserScan` | 10 Hz |
| `/utlidar/cloud`, `/utlidar/cloud_base` | `sensor_msgs/msg/PointCloud2` | 10 Hz |
| `/camera/color/image_raw`, `/camera/color/camera_info` | `sensor_msgs/msg/Image` (640×360 RGB), `CameraInfo` | 15 Hz |
| `/lowstate`, `/sportmodestate` | `unitree_go/msg/LowState`, `SportModeState` | 500 Hz, 50 Hz |
| `/simulation/ground_truth` | `geometry_msgs/msg/PoseStamped` | 50 Hz |
| `/tf`, `/tf_static` | `odom → base_link` and sensor mounts | |

Timestamps use the device's wall clock, so use `use_sim_time=false`. The camera
renders only while an app subscribes to the image topic or the viewer shows
**Robot camera**. The Unitree-native command topics `/api/sport/request` and
`/lowcmd` work too, but your app then needs the `unitree_api` and `unitree_go`
message packages. [compatibility.json](compatibility.json) lists every topic,
command and limit.

Not tested yet: an app container talking to this simulator over ROS 2 on a
device. The steps above follow the simulator's code, whose two processes use
this same configuration to talk to each other inside its container. Wendy's
default ROS 2 settings for `"scope": "device"` are a different configuration,
and we have not checked whether they can see the simulator, so set the
variables in step 3.

## Ports

| Variable | Default | Purpose |
|---|---:|---|
| `APP_ID` | required | Application identifier |
| `PORT` | `8890` | Viewer and HTTP API port, and the readiness check |

If another app on the device already uses port 8890, for example another
simulator, choose a different port when you create the project:

```sh
wendy init my-sim --template go2-sim --var PORT=8891
```

To change it later, edit `GO2_PORT` and `EXPOSE` at the end of the `Dockerfile`
and the readiness `port` in `wendy.json`. Run one simulator per device if you
use ROS 2: every copy publishes the same topics on domain 0.

## How it works

- The `Dockerfile` builds in two stages. The first downloads the pinned robot
  model, walking policy and Unitree message definitions, checks their hashes,
  builds lighter meshes for the viewer and the robot camera, and compiles the
  ROS 2 packages. The final image keeps only what the simulator needs at
  runtime.
- `entrypoint.sh` starts two processes: `go2_command_ingress`, a small C++ ROS 2
  node that receives commands, and `go2_sim.server`, which runs the physics, the
  walking policy, the ROS 2 publishers and the web viewer. If either one stops,
  the container exits.
- `wendy.json` grants host networking and waits for port {{.PORT}}.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 100
wendy device apps stop {{.APP_ID}}
```

- **The 3D view stays empty:** your browser needs WebGL 2. Click **Retry 3D
  viewer**, or use **Robot camera**.
- **Keys do nothing:** click **Enable controls**, then click the 3D view.
- **Your app does not appear under Publisher:** check the four steps in
  "Connect your own app". The app must be publishing on `/cmd_vel` right now.

## Limits

This is a simulation, not Unitree's firmware. The robot walks with a published
reinforcement-learning policy, not Unitree's own controller. The lidar and
camera are virtual and not factory-calibrated, there is no depth camera, and
the battery reading is a constant. Jumps, dances, audio, video streaming and
cloud services are not simulated. See `unsupported` in
[compatibility.json](compatibility.json).

## Licenses and upstream

[UPSTREAM.md](UPSTREAM.md) lists the robot model, walking policy, Unitree
message definitions and Three.js, with their pinned versions. Their license
notices are in [licenses/](licenses/). The build downloads the model, policy
and Unitree sources and checks their hashes; they are not stored in this
project. Three.js is included in `go2_sim/vendor/`.

This project is a copy of `go/simulator/go2` in the WendyOS repository at
commit `a1e1fbf1f3a1a84618d2d79fa835156b38c7a260`. Changes for the template:

- `wendy.json`: your app ID, host networking instead of `host-admin` (the
  admin mode is only for the simulator VM's network isolation, which runs only
  when `GO2_VM_NAME` is set), and readiness on `PORT`.
- `Dockerfile`: `GO2_PORT` and `EXPOSE` use `PORT`; the viewer meshes and their
  lock are generated on the architecture that builds the image; and a CMake
  workaround lets the ROS 2 packages build under ARM64 emulation on x86.
- `go2_sim/vendor/three.core.js` escapes the Go template delimiters in its
  comments so that `wendy init` can render it. The generated file is
  byte-identical to the upstream file listed in UPSTREAM.md.
- Left out: the Go code that embeds the simulator in the Wendy CLI and the
  `.gitignore` lines for Go's vendor tree, the source README, the sample apps
  (`examples/`, `tools/sync_examples.py`), the VM acceptance harnesses and
  their results (`integration/`, `validation/`, `Dockerfile.ros`), and the
  simulator's development tests (`tests/`, `pytest.ini`,
  `requirements-dev.txt`). UPSTREAM.md still mentions some of these; they
  remain in the WendyOS repository.
