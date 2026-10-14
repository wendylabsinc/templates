# {{.APP_ID}}

A browser page for calibrating an SO-101 arm. You move the arm by hand and the
page records, for each joint, where the middle is (the homing offset) and how
far it travels (range minimum and maximum). It saves the result as
LeRobot-convention calibration JSON, which maps raw servo ticks (0 to 4095) to
the normalized joint values LeRobot policies and datasets use.

The arm stays limp the whole time: the app never enables servo torque. It only
pings the servos and reads their positions.

## Requirements

- A WendyOS device with Wendy CLI access
- An SO-101 arm (Feetech STS3215 servos, IDs 1 to 6) whose USB serial adapter
  is plugged into the device, with the arm's servo power supply switched on
- The adapter appearing on the device as `/dev/{{.SERIAL_DEVICE}}`. Check with
  `ls /dev/ttyACM* /dev/ttyUSB*` in a device shell. The usual SO-101 adapter
  board (USB ID `1a86:55d3`) appears as `ttyACM<N>`.
- Network access during the first build to install Python packages

The `serial` entitlement grants the container exactly one tty, and that device
must be connected when you deploy, or the app does not start. If the arm
appears under another name, change `device` in `wendy.json`, or generate the
project again with `--var SERIAL_DEVICE=ttyACM1`.

## Run

```sh
wendy run
```

The post-start hook opens `http://<device-hostname>:{{.PORT}}`. The top line of
the page shows the serial port the app chose, for example
`auto-detected /dev/{{.SERIAL_DEVICE}}: 6/6 servos answered`. If no servo
answers, the page says why (no device visible, or no servo reply) and the app
probes again every 5 seconds, so you can switch on the arm's power without
restarting the app.

## Calibrate

1. Move the arm to its neutral middle pose, then click **Set home**. This sets
   `homing_offset = raw - 2048` for each joint, so the middle pose reads about
   0 afterwards.
2. Click **Start range capture**, move every joint from one end of its travel
   to the other, then click **Stop range capture**.
3. Click **Save** to write the calibration JSON.

The table shows each joint's `raw`, `homed`, and `norm` values. After step 2,
moving a joint swings `norm` from -100 to 100 (the gripper from 0 to 100).

### Is the calibration good enough?

The **Δ** column is the range covered so far (max minus min, in ticks), and the
**status** column grades each joint:

| Status | Meaning |
|---|---|
| `set home` | Home is not set yet; click **Set home** |
| `not swept` | Home is set, but the joint has not moved during range capture |
| `narrow` | The joint moved too little; move it further, to both ends |
| `ok` | Home is set and the range is wide enough |

The header counts the joints with status `ok`: it shows `N/6 incomplete` until
all six are `ok`, then `6/6 sufficient`. Aim for `6/6 sufficient` before
saving. A joint is `narrow` while its range is
below `SO101_MIN_RANGE_TICKS` (1000 ticks; `SO101_MIN_GRIPPER_TICKS`, 300, for
the gripper). Lower the floor for a joint that is mechanically limited.
**Save** still writes the file while joints are `narrow`, and its response
lists them. It refuses to save until every joint has a home and a range.

## Output

**Save** writes `/data/follower.json` inside the container. `/data` is the
persistent volume `{{.APP_ID}}-data`, which is
`/var/lib/wendy/volumes/{{.APP_ID}}-data/` on the device and survives restarts
and redeploys. The file uses LeRobot's per-motor shape, with ranges in the homed
frame:

```json
{
  "shoulder_pan": { "id": 1, "drive_mode": 0, "homing_offset": -312, "range_min": -1024, "range_max": 980 }
}
```

The file has one entry per joint: `shoulder_pan`, `shoulder_lift`,
`elbow_flex`, `wrist_flex`, `wrist_roll`, and `gripper`.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `APP_ID` | required | Application identifier |
| `PORT` | `8090` | Web page, readiness probe, and browser port |
| `SERIAL_DEVICE` | `ttyACM0` | Serial tty granted by the entitlement in `wendy.json` |

Runtime settings come from the environment. The `Dockerfile` sets the defaults:

| Setting | Default | Purpose |
|---|---|---|
| `SO101_CALIB_PORT` | `auto` | Serial port to use; `auto` probes the visible serial devices |
| `SO101_CALIB_ID` | `follower` | Output file name; use `leader` when calibrating a leader arm |
| `SO101_BAUD` | `1000000` | Servo bus baud rate |
| `SO101_SERVO_IDS` | `1,2,3,4,5,6` | Servo IDs, in joint order |
| `SO101_JOINT_NAMES` | `shoulder_pan,…,gripper` | Joint names written to the JSON |
| `SO101_DATA_DIR` | `/data` | Output directory, the persistent volume |

`calibrate.py` also reads `SO101_MIN_RANGE_TICKS`, `SO101_MIN_GRIPPER_TICKS`,
the read-glitch filter settings, and `SO101_PORT_RETRY_SECONDS` (5). Use
`wendy run --env KEY=VALUE` for a one-off override, or edit the `Dockerfile` for
a project default.

## How it works

- `port_detect.py` chooses the serial port. With `SO101_CALIB_PORT=auto`, it
  lists `/dev/serial/by-id/*`, `/dev/ttyACM*`, and `/dev/ttyUSB*`, pings each
  servo ID on every device at the configured baud rate, and picks the device
  where the most servos answer. Inside the container only the entitled tty is
  visible, so the probe confirms that the arm answers there. An explicit path
  in `SO101_CALIB_PORT` skips the probe.
- `calibrate.py` opens the chosen port with the Feetech SDK, reads each servo's
  present position (register 56), and serves the page and its JSON endpoints
  with Flask. During range capture it samples every 20 ms and drops readings
  that jump to the encoder boundary.
- `wendy.json` grants the serial tty, host networking, and the persistent
  volume, and waits up to 30 seconds for port {{.PORT}}.

## Calibrate a second arm

Calibrate one arm at a time. To calibrate a leader arm as well, connect it,
add a second serial entitlement for its tty in `wendy.json`, and set
`SO101_CALIB_ID=leader`. When servos answer on more than one device, the page
names the others; set `SO101_CALIB_PORT` to the device you want.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 100
wendy device apps stop {{.APP_ID}}
```

The log prints the chosen port, or the reason none was usable.

- **The app does not start and the error mentions `serial device ... unavailable`:**
  the entitled tty was not connected at deploy time. Plug in the arm, check its
  tty name, and run `wendy run` again.
- **`0/6 servos answered` or `no Feetech servos answered`:** the USB adapter is
  visible but no servo replied. Switch on the arm's power supply and check the
  cable from the adapter board to the first servo.
- **Some servo IDs do not answer:** check the cables between the servos.
- **A joint moves the wrong way in your policy:** see the caveats below.

## Safety and caveats

- The app never writes to the servos. It does not enable torque, so the arm
  stays back-drivable and safe to move by hand.
- The calibration follows LeRobot's convention (half-turn homing, joints in
  -100 to 100, gripper in 0 to 100), but this is not LeRobot's own code. Check
  it against your installed LeRobot version before you rely on it for a policy.
- `drive_mode` is always `0`. Some SO-101 joints are mounted reversed and need
  `1`; edit the saved JSON for those joints.
- The app writes calibration values only. The consumer (LeRobot or your own
  driver) applies them; for example, LeRobot writes the homing offset to the
  servos when it loads the file.
