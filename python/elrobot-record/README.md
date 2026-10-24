# {{.APP_ID}}

Records ElRobot demonstrations for training a robot policy. ElRobot is
norma-core's 3D-printed arm with 7 joints and a gripper (8 Feetech STS3215
servos). You move the leader arm by hand, the follower arm copies it, and two
USB cameras (one on the arm, one over the table) film the scene. Each episode
is saved as a LeRobot dataset in LeRobot 0.6.1's format (codebase v3.0):
parquet files for the joint data, MP4 videos for the cameras, and a `meta/`
folder. `lerobot-train` and the Hugging Face Hub read this format as it is.

The app runs LeRobot's own recording code (`record_loop` from
`lerobot-record`, `LeRobotDataset`, and its video encoder) with the ElRobot
classes of the community LeRobot integration, vendored in `elrobot_arms.py`.
A container has no terminal for `lerobot-record`'s arrow-key controls, so this
app puts the same controls on a web page. The same page calibrates the arms.

## Requirements

- A WendyOS device with Wendy CLI access
- An ElRobot follower arm (12 V supply) and an ElRobot leader arm (5 to
  7.4 V supply), each with its 8 servos at IDs 1 to 8 and 1,000,000 baud,
  both plugged into the device by USB, with both servo supplies switched on
- Two USB cameras: the arm camera and the table camera
- Network access during the first build to install Python packages (LeRobot
  0.6.1, CPU-only PyTorch)

The `serial` entitlements grant the container exactly the follower's tty
(`{{.FOLLOWER_SERIAL_DEVICE}}`) and the leader's tty
(`{{.LEADER_SERIAL_DEVICE}}`). Both must be connected when you deploy, or the
app does not start. The defaults, `ttyACM1` for the follower and `ttyACM0`
for the leader, are the team's Jetson: a read-only scan found 8 servos at
12.4 V on `ttyACM1` (USB serial `5B3D049367`), and an adapter on `ttyACM0`
(serial `5B61036022`) whose servos did not answer, which is the leader with
its supply off. `ttyACM` numbers follow the order the boards were plugged
in, so check them in a device shell with `ls -l /dev/serial/by-id/`. If they
differ, generate the project again with `--var FOLLOWER_SERIAL_DEVICE=...`
and `--var LEADER_SERIAL_DEVICE=...`, or change `device` in `wendy.json` and
`ELROBOT_FOLLOWER_PORT` / `ELROBOT_LEADER_PORT` in the `Dockerfile`.

Swapped ports are caught before anything moves: the app reads each arm's
supply voltage and refuses a "follower" below 9.5 V or a "leader" above 9 V
(`ELROBOT_CHECK_VOLTAGE=false` turns this off).

## The arm

The joint names are the community integration's; norma-core calls the joints
`rev_motor_01` to `rev_motor_08`. The ranges are the follower's, from
norma-core's URDF. The leader has no URDF; it uses SO-101 parts for its
handle, trigger and wrist roll.

| Servo ID | Joint | Follower range (URDF) |
|---|---|---|
| 1 | `shoulder_yaw` | -88.9 to 88.9 degrees |
| 2 | `shoulder_pitch` | -92.4 to 92.4 degrees |
| 3 | `elbow_yaw` | -100.9 to 100.9 degrees |
| 4 | `elbow_pitch` | -100.5 to 100.5 degrees |
| 5 | `wrist_pitch` | -150.1 to 186.3 degrees |
| 6 | `wrist_roll` | -78.9 to 101.1 degrees |
| 7 | `wrist_yaw` | -183.4 to 156.6 degrees |
| 8 | `gripper` | 0 to 126.2 degrees (each jaw moves 25.5 mm) |

## Calibrate both arms first

LeRobot needs a calibration file for each arm: per joint, a homing offset and
the range of travel. The app reads `follower.json` and `leader.json` from the
persistent volume `{{.CALIBRATION_VOLUME}}`, which `wendy.json` mounts at
`/calibration` (`/var/lib/wendy/volumes/{{.CALIBRATION_VOLUME}}/` on the
device). `elrobot-policy` mounts the same volume.

The team's follower was calibrated by norma-core, which drives every joint
into its end stops and writes homing offsets into the servos that centre each
joint's travel on the encoder's middle (2048), away from its 0/4095 wrap. The
scan's readings fit that. So the app keeps those offsets: its calibration
mode records each joint's range while you move the arm by hand, and stores
the offsets the servos already hold. It never writes to the servos.

1. Calibrate the follower:

   ```sh
   wendy run --env ELROBOT_CALIBRATE=follower
   ```

   The page at `http://<device-hostname>:{{.PORT}}` shows each joint's
   position. The arm must be limp; if the page says torque is on, stop the
   program that holds it (another app, or norma-core's software).
2. Leave **Keep the servos' homing** selected. Press **Start**, move every
   joint slowly from one end of its travel to the other (including
   `wrist_roll` and the gripper), then press **Stop**.
3. Press **Save**. Each joint must show `ok`. `narrow` saves with a warning
   (the joint is then limited to the range you swept). `wrapped` and
   `too wide` (a follower range wider than its URDF range) refuse to save:
   the reading crossed the encoder's 0/4095 boundary.
4. Calibrate the leader the same way with `ELROBOT_CALIBRATE=leader`.

If a joint reports `wrapped` (likely on an arm whose servos still have
factory offsets), put the arm in the middle of every joint's travel and press
**Set home here** before **Start**. This computes new homing offsets that make
that pose read 2047, LeRobot's convention, and puts them in the file only.
The servos still hold the old offsets, so the recorder refuses to start until
you run it once with `ELROBOT_WRITE_CALIBRATION=true`, which writes the
file's offsets and ranges (see below). Doing that to the follower replaces
norma-core's calibration.

A previous file is kept as `<name>.json.<time>.bak`. You can also bring a
file made by `lerobot-calibrate` with the upstream integration: it has the
same format, and that tool has already written its offsets to the servos.
It saves `wrist_roll` as a full turn (0 to 4095), which the app accepts with
a warning; on norma-core's follower, `wrist_roll` has end stops, so this
app records its range like the other joints.

Joint positions are recorded in degrees through these calibrations. A policy
trained on the dataset expects the same calibration on the arm that runs it,
so calibrate once and keep the files.

## What the app writes to the servos

By default the app writes only RAM registers, which reset when the servos
lose power:

| When | Arm | Registers |
|---|---|---|
| Connecting, after the checks below | follower | `Goal_Position` (the present position, so nothing moves), then `Torque_Enable`=1 and `Lock`=1 |
| Every frame while recording | follower | `Goal_Position` |
| End of the session, an error, or app stop | follower | `Goal_Position` (back to the start pose), then `Torque_Enable`=0 and `Lock`=0 |
| Connecting, only if a leader servo has torque on | leader | `Torque_Enable`=0 and `Lock`=0 |
| Releasing the leader at the end | leader | `Torque_Enable`=0 and `Lock`=0 |

LeRobot writes `Lock` together with `Torque_Enable`; `Lock`=0 lets the
servo's EEPROM be written. The dry run and the calibration mode only read.

Two settings allow EEPROM writes. Both are off by default and are logged when
used:

- `ELROBOT_WRITE_CALIBRATION=true`: when an arm's servos do not hold the
  calibration file's values, the app turns that arm's torque off and writes
  `Homing_Offset`, `Min_Position_Limit` and `Max_Position_Limit` from the
  file, as LeRobot does. Without it, the app writes none of them: if only
  the position limits differ (the file keeps the servos' offsets, and the
  servos keep norma-core's 0 to 4095 limits), it limits goals in software;
  if a homing offset differs, it refuses to start, because positions would be
  misread.
- `ELROBOT_CONFIGURE_SERVOS=true`: runs the upstream integration's
  `configure()` when an arm connects. On the follower it writes LeRobot's bus
  defaults (`Return_Delay_Time`=0, `Maximum_Acceleration`=254,
  `Acceleration`=254, and bit 4 of `Phase` cleared if set),
  `Operating_Mode`=0 (position), `P_Coefficient`=16, `I_Coefficient`=0 and
  `D_Coefficient`=32 on every servo, and on the gripper `Max_Torque_Limit`=500,
  `Protection_Current`=250 and `Overload_Torque`=25. On the leader it writes
  the bus defaults and `Operating_Mode`=0. Without it, the follower must
  already be in position mode, or the app refuses to start.

The default keeps the gains the servos have. The scan found P=16, I=0, D=0 on
the follower, norma-core's tuning for ElRobot; D=32 is LeRobot's SO-101
value, which the integration copied.

## Check the setup without moving anything

```sh
wendy run --env ELROBOT_DRY_RUN=true
```

The dry run imports LeRobot, checks both calibration files, writes and reads
back a 2-second synthetic episode in a scratch dataset (this tests the video
encoder and reports its speed), and reads every servo's registers on both
arms: position, voltage, torque, mode, homing offset, position limits, PID
gains and protection settings. It then says what connecting would write. It
captures one picture from each camera and shows it on the page. It only reads
from the servos. The result also appears in `wendy device logs {{.APP_ID}}`.

## Cameras

`ELROBOT_CAMERAS` names each camera: `arm=/dev/video0,table=/dev/video2` by
default. The name becomes `observation.images.<name>` in the dataset. The app
does not know which device is which: the team's Jetson has two identical
"KD-USB Cameras" at `/dev/video0` and `/dev/video2`, and nothing told them
apart. Look at the pictures on the page (from the dry run, and taken again
when each session starts). If the arm camera shows under `table`, swap them:

```sh
wendy run --env ELROBOT_CAMERAS=arm=/dev/video2,table=/dev/video0
```

`/dev/videoN` numbers can change when a camera is plugged in again or the
device reboots; `ls -l /dev/v4l/by-path/` in a device shell shows which USB
port each node belongs to. A USB camera usually has two nodes; the first one
captures images. Keep the names once you record: a policy is trained on them.

## Record

Set the task sentence, the dataset name and the number of episodes:

```sh
wendy run \
  --env ELROBOT_TASK="Pick up the red cube and put it in the bowl." \
  --env ELROBOT_DATASET_REPO_ID=my-team/elrobot-red-cube \
  --env ELROBOT_NUM_EPISODES=30
```

`wendy run` opens `http://<device-hostname>:{{.PORT}}`. The page shows the
current phase with a countdown:

1. **connecting**: the app connects the leader, then the follower. It checks
   both supply voltages and that every follower joint is within its
   calibrated range, holds the follower where it is, turns its torque on, and
   moves it to the leader's pose at about 45 degrees a second (2 to 8 s).
2. **resetting: get ready**: the follower now copies the leader. Set up the
   scene. Recording starts after `ELROBOT_RESET_SECONDS`.
3. **recording**: perform the task with the leader arm. The episode lasts
   `ELROBOT_EPISODE_SECONDS`.
4. **resetting**: put the scene back for the next episode.
5. **saving**: the episode is written to the dataset. Steps 3 to 5 repeat
   until the dataset holds `ELROBOT_NUM_EPISODES` episodes.

The buttons match `lerobot-record`'s keys:

| Button | `lerobot-record` key | What it does |
|---|---|---|
| **Next** | right arrow | Ends the current recording or reset phase now |
| **Re-record** | left arrow | Discards the current episode; it is recorded again after the reset |
| **Stop recording** | Esc | Saves the episode in progress and ends the session |
| **Upload to the Hub** | none | Uploads the dataset again after the session (needs `HF_TOKEN`) |

`ELROBOT_NUM_EPISODES` counts the episodes in the dataset, not in this
session. Recording resumes when the app restarts, so a redeploy or a reboot
continues the same dataset. To record more episodes later, raise
`ELROBOT_NUM_EPISODES`. When the session ends, the follower moves back to the
pose it had when it connected, over `ELROBOT_RETURN_SECONDS`, and its torque
goes off. The page stays up until you stop the app.

## Output

The dataset is written to `/data/lerobot/<ELROBOT_DATASET_REPO_ID>` inside the
container. `/data` is the persistent volume `{{.APP_ID}}-data`, which is
`/var/lib/wendy/volumes/{{.APP_ID}}-data/lerobot/<ELROBOT_DATASET_REPO_ID>`
on the device:

```text
meta/info.json, stats.json, tasks.parquet, episodes/
data/chunk-000/file-000.parquet, ...
videos/observation.images.arm/chunk-000/file-000.mp4, ...
videos/observation.images.table/chunk-000/file-000.mp4, ...
```

The features are `observation.state` and `action` (8 values each, named
`shoulder_yaw.pos` to `gripper.pos`: joints in degrees, the gripper from 0 to
100) and `observation.images.arm` and `observation.images.table`. The robot
type is `elrobot_follower`, so the dataset is marked as ElRobot data. The
action is the leader's position, as in `lerobot-record`.

The app saves each episode and closes the dataset files before it starts the
next one. If the app is stopped or the device loses power, at most the
episode in progress is lost. Because of this, every episode has its own
parquet and MP4 files.

Unlike `lerobot-record`, the app does not add a date and time to the dataset
name, so the name you train on is the name you set.

## Upload to the Hugging Face Hub

Uploading is optional. With `HF_TOKEN` set and a Hub repo ID (not `local/...`)
in `ELROBOT_DATASET_REPO_ID`, the app uploads the dataset with LeRobot's
`push_to_hub` when the session ends, tagged `elrobot` and `wendy`:

```sh
wendy run \
  --env ELROBOT_DATASET_REPO_ID=my-team/elrobot-red-cube \
  --env HF_TOKEN=hf_...
```

The token is stored in the app's configuration on the robot, where anyone with
access to the device can read it. Limit what it can do:

1. Create the dataset repository on the Hub first (huggingface.co, **New
   dataset**), named as in `ELROBOT_DATASET_REPO_ID`.
2. Create a **fine-grained** access token (Settings, Access Tokens) with
   **write access to that one dataset repository only**, and no other
   permissions.
3. Pass that token as `HF_TOKEN`. Revoke it when the recording is done.

The upload creates the dataset as private unless you set
`ELROBOT_DATASET_PRIVATE=false`. A repository that already exists keeps its
visibility. To keep any token off the robot, leave `HF_TOKEN` unset and copy
the dataset folder off the device, then upload it from your computer.

## Train and run a policy

Training needs no ElRobot code: the dataset is a plain LeRobot 0.6.1 dataset,
and `lerobot-train` takes the 8-value state and action and the two cameras
from its `meta/info.json`. ACT and SmolVLA size themselves from those
features, so the `wendy train` image trains it as it is. Train with LeRobot
0.6.1, the version that wrote the dataset, for example:

```sh
lerobot-train \
  --dataset.repo_id=my-team/elrobot-red-cube \
  --policy.type=act \
  --policy.repo_id=my-team/elrobot-red-cube-act \
  --output_dir=outputs/elrobot-red-cube-act
```

Then run the trained model on the follower arm with the `elrobot-policy`
template, with the same calibration and camera names.

## Configuration

Template variables:

| Variable | Default | Purpose |
|---|---|---|
| `APP_ID` | required | Application identifier |
| `PORT` | `8094` | Web page, readiness probe, and browser port |
| `FOLLOWER_SERIAL_DEVICE` | `ttyACM1` | Follower arm tty, granted by `wendy.json` |
| `LEADER_SERIAL_DEVICE` | `ttyACM0` | Leader arm tty, granted by `wendy.json` |
| `CALIBRATION_VOLUME` | `elrobot-calibration` | Volume with `follower.json` and `leader.json`, shared with `elrobot-policy` |

Runtime settings come from the environment. The `Dockerfile` sets the
defaults; override one with `wendy run --env KEY=VALUE`:

| Setting | Default | Purpose |
|---|---|---|
| `ELROBOT_TASK` | `Pick up the cube and place it in the box.` | Task sentence stored with every frame; language-conditioned policies such as SmolVLA use it |
| `ELROBOT_DATASET_REPO_ID` | `local/elrobot_demos` | Dataset name, `<user-or-org>/<name>`; a `local/` name is never uploaded |
| `ELROBOT_NUM_EPISODES` | `50` | Episodes the dataset should hold; recording stops there |
| `ELROBOT_EPISODE_SECONDS` | `30` | Length of one recorded episode |
| `ELROBOT_RESET_SECONDS` | `10` | Time between episodes to reset the scene |
| `ELROBOT_FOLLOWER_PORT` | `/dev/{{.FOLLOWER_SERIAL_DEVICE}}` | Follower arm serial port |
| `ELROBOT_LEADER_PORT` | `/dev/{{.LEADER_SERIAL_DEVICE}}` | Leader arm serial port |
| `ELROBOT_FOLLOWER_CALIBRATION` | `/calibration/follower.json` | Follower calibration file |
| `ELROBOT_LEADER_CALIBRATION` | `/calibration/leader.json` | Leader calibration file |
| `ELROBOT_CALIBRATE` | unset | `follower` or `leader` turns the app into the calibration page for that arm |
| `ELROBOT_WRITE_CALIBRATION` | `false` | Write the calibration file to the servos when they differ (see above) |
| `ELROBOT_CONFIGURE_SERVOS` | `false` | Write the integration's servo settings when connecting (see above) |
| `ELROBOT_CHECK_VOLTAGE` | `true` | Refuse a follower below 9.5 V or a leader above 9 V |
| `ELROBOT_CAMERAS` | `arm=/dev/video0,table=/dev/video2` | Cameras as `name=device`, comma-separated; the name becomes `observation.images.<name>` |
| `ELROBOT_CAMERA_WIDTH`, `ELROBOT_CAMERA_HEIGHT` | `640`, `480` | Capture size of every camera |
| `ELROBOT_CAMERA_FOURCC` | `MJPG` | Camera pixel format; `auto` keeps the camera's default |
| `ELROBOT_FPS` | `30` | Recording rate; the cameras must deliver it |
| `ELROBOT_MAX_RELATIVE_TARGET` | 270 / `ELROBOT_FPS` (9 at 30 fps) | Largest follower move per frame, in joint units (LeRobot's `max_relative_target`); `none` turns it off |
| `ELROBOT_RETURN_SECONDS` | `3` | Time to move back to the start pose at the end (0 to 5; 0 skips it) |
| `HF_TOKEN` | unset | Hugging Face token for the upload (see above) |
| `ELROBOT_PUSH_TO_HUB` | `auto` | `auto` uploads when `HF_TOKEN` and a Hub repo ID are set; `true` requires both; `false` never uploads |
| `ELROBOT_DATASET_PRIVATE` | `true` | Create the Hub repository as private |
| `ELROBOT_DRY_RUN` | `false` | Check everything without recording or moving the arms |
| `ELROBOT_USE_DEGREES` | `true` | Joint units of both arms: degrees (LeRobot 0.6.1's default) or `false` for -100 to 100 |
| `ELROBOT_VIDEO_CODEC` | `libsvtav1` | `libsvtav1` (AV1, LeRobot's default), `h264`, or `hevc` |
| `ELROBOT_STREAMING_ENCODING` | `true` | Encode video while recording, so saving an episode is fast |
| `ELROBOT_ENCODER_THREADS` | `2` | Threads per video encoder |
| `HF_LEROBOT_HOME` | `/data/lerobot` | Parent folder of the dataset |
| `ELROBOT_HTTP_PORT` | `{{.PORT}}` | Web page port |

A dataset cannot change its cameras, image size, frame rate, or joint units
after the first episode. To record with other settings, use a new
`ELROBOT_DATASET_REPO_ID`.

## Safety

- The follower runs 30 kg servos on 12 V and copies the leader at up to the
  servos' speed. Keep hands and objects out of its reach (430 mm), and keep
  the leader within the follower's workspace.
- Torque goes on only when every follower joint is within its calibrated
  range (5 degrees of slack), and at the position it is in, so the arm does
  not jump.
- Follower goals are clamped to each joint's calibrated range, and one frame
  may move a joint at most `ELROBOT_MAX_RELATIVE_TARGET`: by default the
  URDF's 270 degrees a second, so a bad leader reading cannot make the
  follower jump. In degrees, LeRobot itself does not limit goals, and the
  servos' own limits stay 0 to 4095 unless you write the calibration.
- At the end of a session, on an error, and when the app is stopped, the
  follower moves back to where it started, then its torque goes off and it
  falls under gravity. Start it from a rest pose, or support it.
- Stopping the app during an episode discards that episode. Wendy gives an
  app 10 seconds after it asks it to stop, which covers closing the dataset
  and the return, so `ELROBOT_RETURN_SECONDS` is at most 5.
- Run the dry run first on new hardware.

## How it works

- `record_settings.py` reads and checks the settings (standard library only).
- `record.py` builds the ElRobot follower (with the cameras) and leader,
  checks the calibration files, and runs `lerobot-record`'s loop: each frame
  it reads the follower and the cameras, reads the leader, sends the leader's
  position to the follower, and adds the frame to the dataset. Each episode
  is opened with `LeRobotDataset.create` or `LeRobotDataset.resume` and closed
  with `finalize()`.
- `elrobot_arms.py` holds the ElRobot follower and leader classes, vendored
  from the community integration
  [dreamerarun/elrobot_teleoperation](https://github.com/dreamerarun/elrobot_teleoperation)
  (Apache-2.0) and ported to LeRobot 0.6.1. [UPSTREAM.md](UPSTREAM.md) lists
  the changes; the license is in [licenses/](licenses/).
- `elrobot_common.py` subclasses those classes so they never wait for a
  terminal and write only what the table above says. It also holds the
  settings parsing, the calibration checks and calibration page, the web
  server, the pre-torque checks and the stop sequence. The `elrobot-policy`
  template has identical copies of `elrobot_common.py`, `elrobot_arms.py`,
  `UPSTREAM.md` and `licenses/`.
- `wendy.json` grants both serial ttys, the cameras, host networking, the
  dataset volume, and the calibration volume.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 100
wendy device apps stop {{.APP_ID}}
```

- **The app does not start and the error mentions `serial device ... unavailable`:**
  one of the arms was not connected at deploy time. Plug it in, check its tty
  name, and run `wendy run` again.
- **`Missing motor IDs`:** that arm's servos did not answer. Switch on its
  supply (12 V for the follower, 5 to 7.4 V for the leader) and check the
  servo cable.
- **`... run at 5.0 V, but the ElRobot follower runs on 12 V`:** the ports are
  swapped, or the follower's supply is off. Check `/dev/serial/by-id/`.
- **`No follower calibration at /calibration/follower.json`:** calibrate the
  arm with `ELROBOT_CALIBRATE=follower`, and check that `CALIBRATION_VOLUME`
  is the same in both templates.
- **`homing offsets differ`:** the servos were calibrated again (for example
  by norma-core) after the file was saved. Calibrate again with
  `ELROBOT_CALIBRATE`, or write the file with `ELROBOT_WRITE_CALIBRATION=true`.
- **`joints outside their calibrated range`:** move the named joints by hand
  (torque is off) and try again, or calibrate again with a full sweep.
- **Servo errors such as `Incorrect status packet` or `Could not connect on
  port`:** check that no other app, such as `elrobot-policy` or norma-core's
  software, is using the same arm. Only one program can drive an arm at a
  time.
- **The follower moves the wrong way or to the wrong place:** a calibration
  is wrong, or a joint needs `drive_mode: 1` in its file. Calibrate again.
- **`was recorded with different settings`:** the dataset in the volume was
  recorded with other cameras, image size, frame rate or units. Use a new
  `ELROBOT_DATASET_REPO_ID`.
- **`Record loop is running slower than the target FPS`:** the cameras or the
  CPU cannot keep up with two video encoders. Lower `ELROBOT_FPS` or the
  capture size, or set `ELROBOT_VIDEO_CODEC=h264`.
- **The upload fails with 401 or 403:** the token cannot write to that
  dataset repository. The dataset stays on the device; fix the token and press
  **Upload to the Hub**.
