# {{.APP_ID}}

Runs a trained LeRobot policy on an SO-101 follower arm. The policy sees the
arm's joint positions and a USB camera, and sends the arm its next joint
positions, 30 times a second. ACT and SmolVLA policies are supported; the app
reads the policy type from the model's `config.json`.

The model comes from the Hugging Face Hub (or a directory) at an exact
revision, so the arm runs the commit you trained and not whatever the branch
points at later. The app uses LeRobot 0.6.1, the version the `so101-record`
template records with, and runs the same steps as `lerobot-rollout`: LeRobot's
policy class, the pre- and post-processors saved with the model, and its SO
follower robot.

The arm stays limp until you press **Start**. **Stop** brings the arm back to
the pose it started from, then turns torque off.

## Requirements

- A WendyOS device with Wendy CLI access
- An SO-101 follower arm (Feetech STS3215 servos, IDs 1 to 6) plugged into the
  device by USB, with its servo power supply switched on
- The USB camera the model was trained with, mounted where it was during
  training
- The follower's calibration file from the
  `so101-calibration` template
- Network access during the first build, and on first start to download the
  model (it is then cached on the device)

The `serial` entitlement grants the container exactly the follower's tty,
`/dev/{{.FOLLOWER_SERIAL_DEVICE}}`, which must be connected when you deploy. If
the arm appears under another name, change `device` in `wendy.json` and
`SO101_FOLLOWER_PORT` in the `Dockerfile`, or generate the project again with
`--var FOLLOWER_SERIAL_DEVICE=ttyACM1`.

## Calibration

The app reads `follower.json` from the `so101-calibration` app's persistent
volume, `{{.CALIBRATION_VOLUME}}`, which `wendy.json` mounts at
`/calibration`. An app ID of `so101-calibration` writes to
`so101-calibration-data`; for another ID, generate this project with
`--var CALIBRATION_VOLUME=<calibration-app-id>-data`.

Use the calibration of the arm that recorded the training data, or calibrate
this arm the same way. A policy sees joint positions through the calibration,
so a different calibration shifts every position it sees and commands.

## Run

```sh
wendy run
```

`wendy run` opens `http://<device-hostname>:{{.PORT}}`. The page shows the
model, the commit being run, the policy type, and the device (CPU or CUDA).
The first start downloads the model, so it can take a few minutes.

1. Put the arm in a rest pose and clear its workspace.
2. Press **Start**. The app connects the arm, holds it where it is, and hands
   it to the policy.
3. Press **Stop**. The arm moves back to the pose it had at **Start** over
   `SO101_RETURN_SECONDS`, then torque goes off and the arm is limp again.

Stopping the app (`wendy device apps stop`, a redeploy) runs the same stop.

## Choose the model

```sh
wendy run \
  --env SO101_MODEL_PATH=my-team/so101-red-cube-act \
  --env SO101_MODEL_REVISION=main
```

- `SO101_MODEL_PATH` is a Hub model ID (`<owner>/<name>`) or a directory in
  the container that holds a model saved by LeRobot, such as a
  `pretrained_model` folder copied into the app's volume and named
  `/data/<folder>`.
- `SO101_MODEL_REVISION` is a commit, branch or tag of a Hub model. The app
  resolves it to a commit at startup, prints that commit in the log
  (`model <id>, revision <requested> -> commit <sha>`) and on the page, and
  loads the config, weights and processors from that one commit. Unset, it
  resolves `main`. Pin the commit for anything you rely on, for example
  `SO101_MODEL_REVISION=4cbf28905b71f874a9e10123b01099c0e758bfcf` for the
  default model as of October 2026.
- `HF_TOKEN` is needed only for a private model. Use a **fine-grained** token
  with **read access to that one model repository only**: the token is stored
  in the app's configuration on the robot, where anyone with access to the
  device can read it.

If the Hub cannot be reached, a commit in `SO101_MODEL_REVISION` is used
without checking, and a branch name resolves to the copy in the device's
cache, if there is one.

The model must take `observation.state` (six joints) and one or more images
(`observation.images.<camera>`), and output a six-joint `action`. It must have
been trained with LeRobot's processor format (LeRobot 0.4 or later), which
saves `policy_preprocessor.json` and `policy_postprocessor.json` with the
model.

### The default model

The default is
[`TakuyaHiraoka/act_so101_pick_diverse_objects`](https://huggingface.co/TakuyaHiraoka/act_so101_pick_diverse_objects),
a public ACT policy for picking objects with one 640x480 wrist camera. Its
training data used joints in degrees, like LeRobot 0.6.1. A model trained on
someone else's arm, camera mount and scene rarely works well on yours: expect
the arm to move under the policy, not to pick reliably. Record your own
demonstrations with `so101-record` and train on them.

### Cameras

`SO101_CAMERAS` maps camera names to devices, for example
`wrist=/dev/video0,front=/dev/video2`. The names must be the model's camera
names (`observation.images.<name>`); the page and the log list them. For a
model with one camera, a bare device such as `/dev/video0` (the default) gets
the model's camera name.

The camera captures at the size the model was trained with, read from the
model's config, unless you set `SO101_CAMERA_WIDTH` and
`SO101_CAMERA_HEIGHT`.

## Check the setup without moving anything

```sh
wendy run --env SO101_DRY_RUN=true
```

The dry run downloads and loads the model, checks it against the arm and the
cameras, runs the policy on a synthetic image and pose to time one inference on
this device, pings the follower's servos (reads only), and captures one frame
from each camera. It never connects the arm through LeRobot, so torque stays
off.

## CPU or GPU

The image chooses PyTorch when it is built. `wendy run` tells the build
whether the device has CUDA:

| Device | PyTorch | Notes |
|---|---|---|
| x86_64 with an NVIDIA GPU | CUDA 12.8 build | Needs an NVIDIA driver that supports CUDA 12; falls back to the CPU otherwise |
| NVIDIA Jetson (Orin, Thor) | CPU build | PyPI's arm64 CUDA builds do not target Jetson; the policy runs on the CPU |
| Other arm64 and x86_64 | CPU build | |

ACT works on a CPU: it computes a chunk of actions at a time (100 for the
default model), so the arm pauses briefly once per chunk while the next one is
computed. SmolVLA on a CPU
takes seconds per chunk, so expect long pauses; it needs a GPU to run
smoothly. The dry run reports the inference time on your device.
`SO101_POLICY_DEVICE` forces `cpu` or `cuda`.

## Safety

- The arm is limp (torque off) until you press **Start**, unless
  `SO101_AUTOSTART=true`.
- At **Start**, each servo's goal is set to its present position before
  torque goes on, so the arm does not jump.
- `SO101_MAX_RELATIVE_TARGET` (10 degrees by default) caps how far one command
  may move a joint from where it is, which is LeRobot's `max_relative_target`.
  At 30 commands a second this still allows fast moves; lower it for gentler
  motion. A value that is too low makes the arm lag the policy.
- **Stop**, stopping the app, or an error moves the arm back to its start pose
  and turns torque off. The arm then falls under gravity, so start from a rest
  pose. If moving back fails, torque still goes off.
- Wendy gives an app 10 seconds to exit after it asks it to stop, so
  `SO101_RETURN_SECONDS` is at most 8.
- Keep people and objects out of the arm's reach while it runs. A policy can
  move the arm anywhere within its calibrated range.

## Configuration

Template variables:

| Variable | Default | Purpose |
|---|---|---|
| `APP_ID` | required | Application identifier |
| `PORT` | `8093` | Web page, readiness probe, and browser port |
| `FOLLOWER_SERIAL_DEVICE` | `ttyACM0` | Follower arm tty, granted by `wendy.json` |
| `CALIBRATION_VOLUME` | `so101-calibration-data` | Volume with `follower.json` |

Runtime settings come from the environment. The `Dockerfile` sets the
defaults; override one with `wendy run --env KEY=VALUE`:

| Setting | Default | Purpose |
|---|---|---|
| `SO101_MODEL_PATH` | `TakuyaHiraoka/act_so101_pick_diverse_objects` | Hub model ID or directory |
| `SO101_MODEL_REVISION` | unset (`main`) | Commit, branch or tag to load |
| `HF_TOKEN` | unset | Read token for a private model |
| `SO101_TASK` | `Pick up the cube and place it in the box.` | Task sentence for SmolVLA; use the one the training data was recorded with (ACT ignores it) |
| `SO101_CAMERAS` | `/dev/video0` | Cameras as `name=device`, comma-separated |
| `SO101_CAMERA_WIDTH`, `SO101_CAMERA_HEIGHT` | the model's | Capture size |
| `SO101_CAMERA_FOURCC` | `MJPG` | Camera pixel format; `auto` keeps the camera's default |
| `SO101_FPS` | `30` | Control rate; use the frame rate of the training data |
| `SO101_FOLLOWER_PORT` | `/dev/{{.FOLLOWER_SERIAL_DEVICE}}` | Follower arm serial port |
| `SO101_FOLLOWER_CALIBRATION` | `/calibration/follower.json` | Follower calibration file |
| `SO101_USE_DEGREES` | `true` | Joint units the model was trained with: degrees (LeRobot 0.6.1's default) or `false` for -100 to 100 |
| `SO101_MAX_RELATIVE_TARGET` | `10` | Largest move per command, in joint units; `none` turns the cap off |
| `SO101_RETURN_SECONDS` | `3` | Time to move back to the start pose on stop (0 to 8; 0 skips it) |
| `SO101_RUN_SECONDS` | `0` | Stop by itself after this many seconds; 0 runs until **Stop** |
| `SO101_AUTOSTART` | `false` | Start the policy as soon as the model is loaded |
| `SO101_POLICY_DEVICE` | `auto` | `auto`, `cpu`, or `cuda` |
| `SO101_DRY_RUN` | `false` | Check everything without moving the arm |
| `HF_HOME` | `/data/huggingface` | Model cache, in the persistent volume |
| `TORCH_HOME` | `/data/torch` | Cache for the ResNet weights torchvision downloads when an ACT model is built |
| `SO101_HTTP_PORT` | `{{.PORT}}` | Web page port |

`SO101_USE_DEGREES` and `SO101_FPS` must match the training data. A model
trained on data from `so101-record` with its defaults needs neither changed.

## How it works

- `policy_settings.py` reads and checks the settings, resolves the revision,
  and checks the model's inputs and outputs (standard library only).
- `run_policy.py` loads the model's config with
  `PreTrainedConfig.from_pretrained`, picks the policy class for its type, and
  loads the weights and the saved processors at the resolved commit. While
  running, each step reads the arm and the cameras, runs LeRobot's
  `predict_action` (preprocessor, `select_action`, postprocessor), and sends
  the action to the arm.
- LeRobot asks on a terminal before it writes a calibration to the servos. The
  app writes the calibration file instead, with torque off.
- `so101_common.py` holds the environment parsing, the calibration checks, the
  web server, and the stop sequence. The `so101-record` template has an
  identical copy.
- `wendy.json` grants the follower's tty, the cameras, the GPU, host
  networking, the model cache volume, and the calibration volume.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 100
wendy device apps stop {{.APP_ID}}
```

- **`could not resolve <model>@<revision>`:** the model ID or revision does
  not exist, the model is private and `HF_TOKEN` is missing, or the device has
  no internet and no cached copy.
- **Servo errors such as `Incorrect status packet` or `Could not connect on
  port`:** check that no other app, such as `so101-record` or
  `so101-calibration`, is using the same arm. Only one app can drive an arm at
  a time; stop the other app.
- **`SO101_CAMERAS names ..., but the model expects ...`:** name each camera
  as the model does, for example `SO101_CAMERAS=wrist=/dev/video0`.
- **`... is a 'diffusion' policy`:** this image runs ACT and SmolVLA. Other
  types need their LeRobot extra in `requirements.txt` and an entry in
  `SUPPORTED_POLICIES` in `policy_settings.py`.
- **The arm moves to strange poses:** check `SO101_USE_DEGREES`, the
  calibration, and the camera's position and name against the training data.
- **The arm pauses every few seconds:** the policy computes its next chunk of
  actions on the CPU. See "CPU or GPU".

## Caveats

- SmolVLA builds on the SmolVLM2 model, whose config and tokenizer are
  downloaded from `HuggingFaceTB/SmolVLM2-500M-Video-Instruct` on first load.
  `SO101_MODEL_REVISION` pins your model, not that base model.
- The control loop runs one policy call per step, like `lerobot-rollout`'s
  synchronous inference. LeRobot's real-time chunking (`--inference.type=rtc`)
  for slow models is not used.
