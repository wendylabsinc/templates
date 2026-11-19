# {{.APP_ID}}

Records SO-101 demonstrations for training a robot policy. You move the leader
arm by hand, the follower arm copies it, and a USB camera films the scene. Each
episode is saved as a LeRobot dataset in LeRobot 0.6.1's format (codebase
v3.0): parquet files for the joint data, MP4 videos for the camera, and a
`meta/` folder. `lerobot-train` and the Hugging Face Hub read this format as
it is.

The app runs LeRobot's own recording code (`record_loop` from
`lerobot-record`, `LeRobotDataset`, and its video encoder). A container has no
terminal for `lerobot-record`'s arrow-key controls, so this app puts the same
controls on a web page.

## Requirements

- A WendyOS device with Wendy CLI access
- An SO-101 follower arm and an SO-101 leader arm (Feetech STS3215 servos,
  IDs 1 to 6), both plugged into the device by USB, with the follower's servo
  power supply switched on
- One USB camera
- Calibration files for both arms from the
  `so101-calibration` template (see below)
- Network access during the first build to install Python packages (LeRobot
  0.6.1, CPU-only PyTorch)

Check the device names in a device shell: `ls -l /dev/serial/by-id/` shows
which `ttyACM<N>` is which arm, and `ls /dev/video*` lists cameras. A USB
camera usually has two `/dev/video` nodes; the first one captures images.

The `serial` entitlements grant the container exactly the follower's tty
(`{{.FOLLOWER_SERIAL_DEVICE}}`) and the leader's tty
(`{{.LEADER_SERIAL_DEVICE}}`). Both arms must be connected when you deploy, or
the app does not start. If the names differ, change `device` in `wendy.json`
and `SO101_FOLLOWER_PORT` / `SO101_LEADER_PORT` in the `Dockerfile`, or
generate the project again with `--var FOLLOWER_SERIAL_DEVICE=ttyACM1`.

## Calibrate both arms first

The recorder reads `follower.json` and `leader.json` from the
`so101-calibration` app's persistent volume, `{{.CALIBRATION_VOLUME}}`, which
`wendy.json` mounts at `/calibration`. Volumes are shared between apps by
name, so no copying is needed.

1. Create and run a calibration app with the app ID that matches the volume
   name: an app ID of `so101-calibration` writes to `so101-calibration-data`.
   If your calibration app has another ID, generate this project with
   `--var CALIBRATION_VOLUME=<calibration-app-id>-data`.
2. Calibrate the follower. It saves `follower.json`.
3. Calibrate the leader with `SO101_CALIB_ID=leader`, so it saves
   `leader.json` (see "Calibrate a second arm" in that template's README).

A dataset records joint positions in degrees, using these calibrations. A
policy trained on it expects the same calibration on the arm that runs it.

## Check the setup without moving anything

```sh
wendy run --env SO101_DRY_RUN=true
```

The dry run imports LeRobot, checks both calibration files, writes and reads
back a 2-second synthetic episode in a scratch dataset (this tests the video
encoder), pings both arms' servos, captures one camera frame, and states
whether the dataset would be uploaded. It only reads from the servos. The
result appears on the web page and in `wendy device logs {{.APP_ID}}`.

## Record

Set the task sentence, the dataset name and the number of episodes:

```sh
wendy run \
  --env SO101_TASK="Pick up the red cube and put it in the bowl." \
  --env SO101_DATASET_REPO_ID=my-team/so101-red-cube \
  --env SO101_NUM_EPISODES=30
```

`wendy run` opens `http://<device-hostname>:{{.PORT}}`. The page shows the
current phase with a countdown:

1. **connecting**: the app connects the leader, then the follower, and moves
   the follower slowly (2 seconds) to the leader's pose.
2. **resetting: get ready**: the follower now copies the leader. Set up the
   scene. Recording starts after `SO101_RESET_SECONDS`.
3. **recording**: perform the task with the leader arm. The episode lasts
   `SO101_EPISODE_SECONDS`.
4. **resetting**: put the scene back for the next episode.
5. **saving**: the episode is written to the dataset. Steps 3 to 5 repeat
   until the dataset holds `SO101_NUM_EPISODES` episodes.

The buttons match `lerobot-record`'s keys:

| Button | `lerobot-record` key | What it does |
|---|---|---|
| **Next** | right arrow | Ends the current recording or reset phase now |
| **Re-record** | left arrow | Discards the current episode; it is recorded again after the reset |
| **Stop recording** | Esc | Saves the episode in progress and ends the session |
| **Upload to the Hub** | none | Uploads the dataset again after the session (needs `HF_TOKEN`) |

`SO101_NUM_EPISODES` counts the episodes in the dataset, not in this session.
Recording resumes when the app restarts, so a redeploy or a reboot continues
the same dataset. To record more episodes later, raise `SO101_NUM_EPISODES`.
After the session ends, the follower is released (torque off, so it goes
limp) and the page stays up until you stop the app.

## Output

The dataset is written to `/data/lerobot/<SO101_DATASET_REPO_ID>` inside the
container. `/data` is the persistent volume `{{.APP_ID}}-data`, which is
`/var/lib/wendy/volumes/{{.APP_ID}}-data/lerobot/<SO101_DATASET_REPO_ID>` on the
device:

```text
meta/info.json, stats.json, tasks.parquet, episodes/
data/chunk-000/file-000.parquet, ...
videos/observation.images.front/chunk-000/file-000.mp4, ...
```

The features are `observation.state` and `action` (six joints each, in
degrees, with the gripper from 0 to 100) and `observation.images.<camera>`.
The robot type is `so_follower`.

The app saves each episode and closes the dataset files before it starts the
next one. If the app is stopped or the device loses power, at most the episode
in progress is lost. Because of this, every episode has its own parquet and
MP4 files.

Unlike `lerobot-record`, the app does not add a date and time to the dataset
name, so the name you train on is the name you set.

## Upload to the Hugging Face Hub

Uploading is optional. With `HF_TOKEN` set and a Hub repo ID (not `local/...`)
in `SO101_DATASET_REPO_ID`, the app uploads the dataset with LeRobot's
`push_to_hub` when the session ends:

```sh
wendy run \
  --env SO101_DATASET_REPO_ID=my-team/so101-red-cube \
  --env HF_TOKEN=hf_...
```

The token is stored in the app's configuration on the robot, where anyone with
access to the device can read it. Limit what it can do:

1. Create the dataset repository on the Hub first (huggingface.co, **New
   dataset**), named as in `SO101_DATASET_REPO_ID`.
2. Create a **fine-grained** access token (Settings, Access Tokens) with
   **write access to that one dataset repository only**, and no other
   permissions.
3. Pass that token as `HF_TOKEN`. Revoke it when the recording is done.

The upload creates the dataset as private unless you set
`SO101_DATASET_PRIVATE=false`. A repository that already exists keeps its
visibility. To keep any token off the robot, leave `HF_TOKEN` unset and copy
the dataset folder off the device, then upload it from your computer.

## Train and run a policy

Train with LeRobot 0.6.1, the version that wrote the dataset, for example:

```sh
lerobot-train \
  --dataset.repo_id=my-team/so101-red-cube \
  --policy.type=act \
  --policy.repo_id=my-team/so101-red-cube-act \
  --output_dir=outputs/so101-red-cube-act
```

Then run the trained model on the follower arm with the
`so101-policy` template.

## Configuration

Template variables:

| Variable | Default | Purpose |
|---|---|---|
| `APP_ID` | required | Application identifier |
| `PORT` | `8092` | Web page, readiness probe, and browser port |
| `FOLLOWER_SERIAL_DEVICE` | `ttyACM0` | Follower arm tty, granted by `wendy.json` |
| `LEADER_SERIAL_DEVICE` | `ttyACM1` | Leader arm tty, granted by `wendy.json` |
| `CALIBRATION_VOLUME` | `so101-calibration-data` | Volume with `follower.json` and `leader.json` |

Runtime settings come from the environment. The `Dockerfile` sets the
defaults; override one with `wendy run --env KEY=VALUE`:

| Setting | Default | Purpose |
|---|---|---|
| `SO101_TASK` | `Pick up the cube and place it in the box.` | Task sentence stored with every frame; language-conditioned policies such as SmolVLA use it |
| `SO101_DATASET_REPO_ID` | `local/so101_demos` | Dataset name, `<user-or-org>/<name>`; a `local/` name is never uploaded |
| `SO101_NUM_EPISODES` | `50` | Episodes the dataset should hold; recording stops there |
| `SO101_EPISODE_SECONDS` | `30` | Length of one recorded episode |
| `SO101_RESET_SECONDS` | `10` | Time between episodes to reset the scene |
| `SO101_FOLLOWER_PORT` | `/dev/{{.FOLLOWER_SERIAL_DEVICE}}` | Follower arm serial port |
| `SO101_LEADER_PORT` | `/dev/{{.LEADER_SERIAL_DEVICE}}` | Leader arm serial port |
| `SO101_FOLLOWER_CALIBRATION` | `/calibration/follower.json` | Follower calibration file |
| `SO101_LEADER_CALIBRATION` | `/calibration/leader.json` | Leader calibration file |
| `SO101_CAMERAS` | `front=/dev/video0` | Cameras as `name=device`, comma-separated; the name becomes `observation.images.<name>` |
| `SO101_CAMERA_WIDTH`, `SO101_CAMERA_HEIGHT` | `640`, `480` | Capture size |
| `SO101_CAMERA_FOURCC` | `MJPG` | Camera pixel format; `auto` keeps the camera's default |
| `SO101_FPS` | `30` | Recording rate; the camera must deliver it |
| `HF_TOKEN` | unset | Hugging Face token for the upload (see above) |
| `SO101_PUSH_TO_HUB` | `auto` | `auto` uploads when `HF_TOKEN` and a Hub repo ID are set; `true` requires both; `false` never uploads |
| `SO101_DATASET_PRIVATE` | `true` | Create the Hub repository as private |
| `SO101_DRY_RUN` | `false` | Check everything without recording or moving the arms |
| `SO101_USE_DEGREES` | `true` | Joint units: degrees (LeRobot 0.6.1's default) or `false` for -100 to 100 |
| `SO101_VIDEO_CODEC` | `libsvtav1` | `libsvtav1` (AV1, LeRobot's default), `h264`, or `hevc` |
| `SO101_STREAMING_ENCODING` | `true` | Encode video while recording, so saving an episode is fast |
| `SO101_ENCODER_THREADS` | `2` | Threads per video encoder |
| `HF_LEROBOT_HOME` | `/data/lerobot` | Parent folder of the dataset |
| `SO101_HTTP_PORT` | `{{.PORT}}` | Web page port |

A dataset cannot change its cameras, image size, frame rate, or joint units
after the first episode. To record with other settings, use a new
`SO101_DATASET_REPO_ID`.

## How it works

- `record_settings.py` reads and checks the settings (standard library only).
- `record.py` builds LeRobot's SO-101 follower (with the cameras) and leader,
  checks the calibration files, and runs `lerobot-record`'s loop: each frame it
  reads the follower and the camera, reads the leader, sends the leader's
  position to the follower, and adds the frame to the dataset. Each episode is
  opened with `LeRobotDataset.create` or `LeRobotDataset.resume` and closed
  with `finalize()`.
- LeRobot asks on a terminal before it writes a calibration to the servos. The
  app writes the calibration file instead, with torque off. It also sets each
  follower servo's goal to its present position before LeRobot turns torque
  on, so the arm does not jump.
- `so101_common.py` holds the environment parsing, the calibration checks, the
  web server, and the code that releases the arms. The `so101-policy` template
  has an identical copy.
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
- **Servo errors such as `Incorrect status packet` or `Could not connect on
  port`:** check that no other app, such as `so101-calibration` or
  `so101-policy`, is using the same arm. Only one app can drive an arm at a
  time; stop the other app.
- **`No follower calibration at /calibration/follower.json`:** calibrate the
  arm with `so101-calibration`, and check that `CALIBRATION_VOLUME` matches
  that app's ID.
- **The follower moves the wrong way or to the wrong place:** the follower or
  leader calibration is wrong, or a joint needs `drive_mode: 1`. Calibrate
  again.
- **`was recorded with different settings`:** the dataset in the volume was
  recorded with other cameras, image size, frame rate or units. Use a new
  `SO101_DATASET_REPO_ID`.
- **`Record loop is running slower than the target FPS`:** the camera or the
  CPU cannot keep up. Lower `SO101_FPS` or the capture size, or set
  `SO101_VIDEO_CODEC=h264`.
- **The upload fails with 401 or 403:** the token cannot write to that
  dataset repository. The dataset stays on the device; fix the token and press
  **Upload to the Hub**.

## Safety and caveats

- The follower copies the leader at full servo speed. Keep hands and objects
  out of its reach, and keep the leader within the follower's workspace.
- When the session ends or the app stops, the follower's torque is turned off,
  so it falls under gravity. Lower it first, or support it.
- Stopping the app during an episode discards that episode. The app has 10
  seconds to close the dataset after Wendy asks it to stop.
- Run the dry run first on new hardware.
