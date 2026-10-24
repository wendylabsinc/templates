# {{.APP_ID}}

Runs a trained LeRobot policy on an ElRobot follower arm (norma-core's arm
with 7 joints and a gripper, 8 Feetech STS3215 servos). The policy sees the
arm's 8 joint positions and its two cameras (the arm camera and the table
camera), and sends the arm its next joint positions, 30 times a second. ACT
and SmolVLA policies are supported; the app reads the policy type from the
model's `config.json`.

The model comes from the Hugging Face Hub (or a directory) at an exact
revision, so the arm runs the commit you trained and not whatever the branch
points at later. The app uses LeRobot 0.6.1, the version the `elrobot-record`
template records with, and runs the same steps as `lerobot-rollout`:
LeRobot's policy class, the pre- and post-processors saved with the model, and
the ElRobot follower class of the community LeRobot integration, vendored in
`elrobot_arms.py`.

The arm stays limp until you press **Start**. **Stop** brings the arm back to
the pose it started from, then turns torque off.

## Requirements

- A WendyOS device with Wendy CLI access
- An ElRobot follower arm (12 V supply, servos at IDs 1 to 8 and 1,000,000
  baud) plugged into the device by USB, with its supply switched on
- The two USB cameras the model was trained with, mounted where they were
  during training
- The follower's calibration file, in the volume `elrobot-record` uses (see
  below)
- A model trained on ElRobot data: there is no public one. Record
  demonstrations with `elrobot-record` and train on them.
- Network access during the first build, and on first start to download the
  model (it is then cached on the device)

The `serial` entitlement grants the container exactly the follower's tty,
`/dev/{{.FOLLOWER_SERIAL_DEVICE}}`, which must be connected when you deploy.
The default, `ttyACM1`, is the team's Jetson, where the leader is on
`ttyACM0`. `ttyACM` numbers follow the order the boards were plugged in, so
check them in a device shell with `ls -l /dev/serial/by-id/`. If the arm
appears under another name, generate the project again with
`--var FOLLOWER_SERIAL_DEVICE=...`, or change `device` in `wendy.json` and
`ELROBOT_FOLLOWER_PORT` in the `Dockerfile`. The app reads the servos' supply
voltage and refuses an arm below 9.5 V, which is the leader (5 to 7.4 V) or a
follower with its supply off (`ELROBOT_CHECK_VOLTAGE=false` turns this off).

## Calibration

The app reads `follower.json` from the persistent volume
`{{.CALIBRATION_VOLUME}}`, which `wendy.json` mounts at `/calibration`. It is
the volume `elrobot-record` uses, so a follower calibrated there is ready
here. Use the calibration of the arm that recorded the training data: a
policy sees joint positions through the calibration, so a different one
shifts every position it sees and commands.

To calibrate the follower with this app (only the follower needs to be
connected):

```sh
wendy run --env ELROBOT_CALIBRATE=follower
```

The page at `http://<device-hostname>:{{.PORT}}` records each joint's range
while you move the limp arm by hand: press **Start**, move every joint from
one end of its travel to the other, **Stop**, then **Save**. It keeps the
homing offsets the servos hold (norma-core's, on the team's follower) and
never writes to the servos. The `elrobot-record` README explains the page,
including **Set home here** for an arm whose joint readings cross the
encoder's 0/4095 boundary.

## What the app writes to the servos

By default the app writes only RAM registers, which reset when the servos
lose power:

| When | Registers |
|---|---|
| **Start**, after the checks below | `Goal_Position` (the present position, so nothing moves), then `Torque_Enable`=1 and `Lock`=1 |
| Every control step | `Goal_Position` |
| **Stop**, an error, or app stop | `Goal_Position` (back to the start pose), then `Torque_Enable`=0 and `Lock`=0 |

LeRobot writes `Lock` together with `Torque_Enable`; `Lock`=0 lets the
servo's EEPROM be written. The dry run and the calibration mode only read.

Two settings allow EEPROM writes. Both are off by default and are logged when
used:

- `ELROBOT_WRITE_CALIBRATION=true`: when the servos do not hold the
  calibration file's values, the app turns torque off and writes
  `Homing_Offset`, `Min_Position_Limit` and `Max_Position_Limit` from the
  file, as LeRobot does. Without it, the app writes none of them: if only the
  position limits differ (the servos keep norma-core's 0 to 4095 limits), it
  limits goals in software; if a homing offset differs, **Start** fails,
  because positions would be misread.
- `ELROBOT_CONFIGURE_SERVOS=true`: runs the upstream integration's
  `configure()` at **Start**: LeRobot's bus defaults (`Return_Delay_Time`=0,
  `Maximum_Acceleration`=254, `Acceleration`=254, and bit 4 of `Phase`
  cleared if set), `Operating_Mode`=0 (position), `P_Coefficient`=16,
  `I_Coefficient`=0 and `D_Coefficient`=32 on every servo, and on the gripper
  `Max_Torque_Limit`=500, `Protection_Current`=250 and `Overload_Torque`=25.
  Without it, the servos must already be in position mode, or **Start**
  fails.

The default keeps the gains the servos have: P=16, I=0, D=0 on the team's
follower, norma-core's tuning for ElRobot. D=32 is LeRobot's SO-101 value,
which the integration copied. Run the policy with the gains the training data
was recorded with.

## Run

```sh
wendy run --env ELROBOT_MODEL_PATH=my-team/elrobot-red-cube-act
```

`wendy run` opens `http://<device-hostname>:{{.PORT}}`. The page shows the
model, the commit being run, the policy type, and the device (CPU or CUDA).
The first start downloads the model, so it can take a few minutes.

1. Put the arm in a rest pose and clear its workspace.
2. Press **Start**. The app connects the arm, checks the supply voltage, the
   servo mode, and that every joint is within its calibrated range, holds the
   arm where it is, turns torque on, takes a picture from each camera, and
   hands the arm to the policy.
3. Press **Stop**. The arm moves back to the pose it had at **Start** over
   `ELROBOT_RETURN_SECONDS`, then torque goes off and the arm is limp again.

Stopping the app (`wendy device apps stop`, a redeploy) runs the same stop.

## Choose the model

```sh
wendy run \
  --env ELROBOT_MODEL_PATH=my-team/elrobot-red-cube-act \
  --env ELROBOT_MODEL_REVISION=main
```

- `ELROBOT_MODEL_PATH` is a Hub model ID (`<owner>/<name>`) or a directory in
  the container that holds a model saved by LeRobot, such as a
  `pretrained_model` folder copied into the app's volume and named
  `/data/<folder>`. It has no default.
- `ELROBOT_MODEL_REVISION` is a commit, branch or tag of a Hub model. The app
  resolves it to a commit at startup, prints that commit in the log
  (`model <id>, revision <requested> -> commit <sha>`) and on the page, and
  loads the config, weights and processors from that one commit. Unset, it
  resolves `main`. Pin the commit for anything you rely on.
- `HF_TOKEN` is needed only for a private model. Use a **fine-grained** token
  with **read access to that one model repository only**: the token is stored
  in the app's configuration on the robot, where anyone with access to the
  device can read it.

If the Hub cannot be reached, a commit in `ELROBOT_MODEL_REVISION` is used
without checking, and a branch name resolves to the copy in the device's
cache, if there is one.

The model must take `observation.state` (8 joints) and one or more images
(`observation.images.<camera>`), and output an 8-joint `action`. An SO-101
model (6 joints) is refused. It must have been trained with LeRobot's
processor format (LeRobot 0.4 or later), which saves
`policy_preprocessor.json` and `policy_postprocessor.json` with the model.
A model trained by the `wendy train` image on an `elrobot-record` dataset
meets all of this; training needs no ElRobot code.

### Cameras

`ELROBOT_CAMERAS` maps camera names to devices:
`arm=/dev/video0,table=/dev/video2` by default, the names `elrobot-record`
uses. The names must be the model's camera names (`observation.images.<name>`);
the page and the log list them. The app does not know which device is which:
the team's Jetson has two identical "KD-USB Cameras" at `/dev/video0` and
`/dev/video2`. Check the pictures on the page (from the dry run, and taken
again at each **Start**), and swap the devices if the arm camera shows under
`table`. `/dev/videoN` numbers can change when a camera is plugged in again or
the device reboots; `ls -l /dev/v4l/by-path/` in a device shell shows which
USB port each node belongs to.

The cameras capture at the size the model was trained with, read from the
model's config, unless you set `ELROBOT_CAMERA_WIDTH` and
`ELROBOT_CAMERA_HEIGHT`.

## Check the setup without moving anything

```sh
wendy run --env ELROBOT_MODEL_PATH=my-team/elrobot-red-cube-act --env ELROBOT_DRY_RUN=true
```

The dry run downloads and loads the model, checks it against the arm and the
cameras, runs the policy on a synthetic image and pose to time one inference
on this device, reads every follower servo's registers (position, voltage,
torque, mode, homing offset, position limits, PID gains, protection settings)
and says what **Start** would write, and captures one picture from each
camera, shown on the page. It never connects the arm through LeRobot, so
torque stays off, and it only reads from the servos.

## CPU or GPU

The image chooses PyTorch when it is built. `wendy run` tells the build
whether the device has CUDA:

| Device | PyTorch | Notes |
|---|---|---|
| x86_64 with an NVIDIA GPU | CUDA 12.8 build | Needs an NVIDIA driver that supports CUDA 12; falls back to the CPU otherwise |
| NVIDIA Jetson (Orin, Thor) | CPU build | PyPI's arm64 CUDA builds do not target Jetson; the policy runs on the CPU |
| Other arm64 and x86_64 | CPU build | |

ACT works on a CPU: it computes a chunk of actions at a time, so the arm
pauses briefly once per chunk while the next one is computed. Two cameras
make each chunk slower than one. SmolVLA on a CPU takes seconds per chunk, so
expect long pauses; it needs a GPU to run smoothly. The dry run reports the
inference time on your device. `ELROBOT_POLICY_DEVICE` forces `cpu` or `cuda`.

## Safety

- The arm is limp (torque off) until you press **Start**, unless
  `ELROBOT_AUTOSTART=true`.
- At **Start**, torque goes on only when every joint is within its calibrated
  range (5 degrees of slack), and each servo's goal is set to its present
  position first, so the arm does not jump.
- Every goal is clamped to the joint's calibrated range. In degrees, LeRobot
  itself does not limit goals, and the servos' own limits stay 0 to 4095
  unless you write the calibration.
- `ELROBOT_MAX_RELATIVE_TARGET` caps how far one command may move a joint
  from where it is (LeRobot's `max_relative_target`). By default it is the
  URDF's joint speed limit (270 degrees a second) divided by `ELROBOT_FPS`:
  9 degrees at 30 commands a second. Lower it for gentler motion; a value that
  is too low makes the arm lag the policy.
- **Stop**, stopping the app, or an error moves the arm back to its start pose
  and turns torque off. The arm then falls under gravity, so start from a rest
  pose. If moving back fails, torque still goes off.
- Wendy gives an app 10 seconds to exit after it asks it to stop, so
  `ELROBOT_RETURN_SECONDS` is at most 8.
- The follower runs 30 kg servos on 12 V and reaches 430 mm. Keep people and
  objects out of its reach while it runs: a policy can move the arm anywhere
  within its calibrated range.

## Configuration

Template variables:

| Variable | Default | Purpose |
|---|---|---|
| `APP_ID` | required | Application identifier |
| `PORT` | `8095` | Web page, readiness probe, and browser port |
| `FOLLOWER_SERIAL_DEVICE` | `ttyACM1` | Follower arm tty, granted by `wendy.json` |
| `CALIBRATION_VOLUME` | `elrobot-calibration` | Volume with `follower.json`, shared with `elrobot-record` |

Runtime settings come from the environment. The `Dockerfile` sets the
defaults; override one with `wendy run --env KEY=VALUE`:

| Setting | Default | Purpose |
|---|---|---|
| `ELROBOT_MODEL_PATH` | unset (required) | Hub model ID or directory |
| `ELROBOT_MODEL_REVISION` | unset (`main`) | Commit, branch or tag to load |
| `HF_TOKEN` | unset | Read token for a private model |
| `ELROBOT_TASK` | `Pick up the cube and place it in the box.` | Task sentence for SmolVLA; use the one the training data was recorded with (ACT ignores it) |
| `ELROBOT_CAMERAS` | `arm=/dev/video0,table=/dev/video2` | Cameras as `name=device`, comma-separated |
| `ELROBOT_CAMERA_WIDTH`, `ELROBOT_CAMERA_HEIGHT` | the model's | Capture size |
| `ELROBOT_CAMERA_FOURCC` | `MJPG` | Camera pixel format; `auto` keeps the camera's default |
| `ELROBOT_FPS` | `30` | Control rate; use the frame rate of the training data |
| `ELROBOT_FOLLOWER_PORT` | `/dev/{{.FOLLOWER_SERIAL_DEVICE}}` | Follower arm serial port |
| `ELROBOT_FOLLOWER_CALIBRATION` | `/calibration/follower.json` | Follower calibration file |
| `ELROBOT_CALIBRATE` | unset | `follower` turns the app into the calibration page |
| `ELROBOT_WRITE_CALIBRATION` | `false` | Write the calibration file to the servos when they differ (see above) |
| `ELROBOT_CONFIGURE_SERVOS` | `false` | Write the integration's servo settings at **Start** (see above) |
| `ELROBOT_CHECK_VOLTAGE` | `true` | Refuse an arm below 9.5 V |
| `ELROBOT_USE_DEGREES` | `true` | Joint units the model was trained with: degrees (LeRobot 0.6.1's default) or `false` for -100 to 100 |
| `ELROBOT_MAX_RELATIVE_TARGET` | 270 / `ELROBOT_FPS` (9 at 30 fps) | Largest move per command, in joint units; `none` turns the cap off |
| `ELROBOT_RETURN_SECONDS` | `3` | Time to move back to the start pose on stop (0 to 8; 0 skips it) |
| `ELROBOT_RUN_SECONDS` | `0` | Stop by itself after this many seconds; 0 runs until **Stop** |
| `ELROBOT_AUTOSTART` | `false` | Start the policy as soon as the model is loaded |
| `ELROBOT_POLICY_DEVICE` | `auto` | `auto`, `cpu`, or `cuda` |
| `ELROBOT_DRY_RUN` | `false` | Check everything without moving the arm |
| `HF_HOME` | `/data/huggingface` | Model cache, in the persistent volume |
| `TORCH_HOME` | `/data/torch` | Cache for the ResNet weights torchvision downloads when an ACT model is built |
| `ELROBOT_HTTP_PORT` | `{{.PORT}}` | Web page port |

`ELROBOT_USE_DEGREES`, `ELROBOT_FPS` and the camera names must match the
training data. A model trained on data from `elrobot-record` with its defaults
needs none of them changed.

## How it works

- `policy_settings.py` reads and checks the settings, resolves the revision,
  and checks the model's inputs and outputs (standard library only).
- `run_policy.py` loads the model's config with
  `PreTrainedConfig.from_pretrained`, picks the policy class for its type, and
  loads the weights and the saved processors at the resolved commit. While
  running, each step reads the arm and the cameras, runs LeRobot's
  `predict_action` (preprocessor, `select_action`, postprocessor), and sends
  the action to the arm.
- `elrobot_arms.py` holds the ElRobot follower class, vendored from the
  community integration
  [dreamerarun/elrobot_teleoperation](https://github.com/dreamerarun/elrobot_teleoperation)
  (Apache-2.0) and ported to LeRobot 0.6.1. [UPSTREAM.md](UPSTREAM.md) lists
  the changes; the license is in [licenses/](licenses/).
- `elrobot_common.py` subclasses it so it never waits for a terminal and
  writes only what the table above says. It also holds the settings parsing,
  the calibration checks and calibration page, the web server, the pre-torque
  checks and the stop sequence. The `elrobot-record` template has identical
  copies of `elrobot_common.py`, `elrobot_arms.py`, `UPSTREAM.md` and
  `licenses/`.
- `wendy.json` grants the follower's tty, the cameras, the GPU, host
  networking, the model cache volume, and the calibration volume.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 100
wendy device apps stop {{.APP_ID}}
```

- **`ELROBOT_MODEL_PATH is not set`:** set it to your trained model.
- **`could not resolve <model>@<revision>`:** the model ID or revision does
  not exist, the model is private and `HF_TOKEN` is missing, or the device has
  no internet and no cached copy.
- **`... does not take an 8-joint observation.state`:** the model was not
  trained on ElRobot data.
- **`ELROBOT_CAMERAS names ..., but the model expects ...`:** name each camera
  as the model does, for example `ELROBOT_CAMERAS=arm=/dev/video0,table=/dev/video2`.
- **`Missing motor IDs`, or `... run at 5.0 V`:** the follower's 12 V supply is
  off, or the port is the leader's. Check `/dev/serial/by-id/`.
- **`homing offsets differ` or `No follower calibration`:** calibrate with
  `ELROBOT_CALIBRATE=follower`, or copy the recording arm's `follower.json`
  into the volume.
- **`joints outside their calibrated range`:** move the named joints by hand
  (torque is off) and press **Start** again.
- **Servo errors such as `Incorrect status packet` or `Could not connect on
  port`:** check that no other app, such as `elrobot-record` or norma-core's
  software, is using the same arm. Only one program can drive an arm at a
  time.
- **`... is a 'diffusion' policy`:** this image runs ACT and SmolVLA. Other
  types need their LeRobot extra in `requirements.txt` and an entry in
  `SUPPORTED_POLICIES` in `policy_settings.py`.
- **The arm moves to strange poses:** check `ELROBOT_USE_DEGREES`, the
  calibration, and the cameras' positions and names against the training
  data.
- **The arm pauses every few seconds:** the policy computes its next chunk of
  actions on the CPU. See "CPU or GPU".

## Caveats

- SmolVLA builds on the SmolVLM2 model, whose config and tokenizer are
  downloaded from `HuggingFaceTB/SmolVLM2-500M-Video-Instruct` on first load.
  `ELROBOT_MODEL_REVISION` pins your model, not that base model.
- The control loop runs one policy call per step, like `lerobot-rollout`'s
  synchronous inference. LeRobot's real-time chunking (`--inference.type=rtc`)
  for slow models is not used.
