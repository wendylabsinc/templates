# Tests

Run the repository checks from the repository root:

```sh
python3 -m pytest tests/
```

CI runs the same command on every pull request.

## Coverage

- `test_cuda_compat_parity.py` checks that the JetPack CUDA compatibility block
  is present in the expected Dockerfiles and remains byte-identical.
- `test_template_readmes.py` checks catalog directories, project README
  coverage, supported README placeholders, relative Markdown links, and
  shared-source drift in both directions.
- `test_so101_calibration.py` checks the `so101-calibration` serial-port
  auto-detection with fake ports, and that the app never writes to the servos.
- `test_so101_sim.py` checks the `so101-sim` variables, the `{{.APP_ID}}` and
  `{{.PORT}}` wiring in `wendy.json`, the `Dockerfile` and the startup message,
  and that `wendy init` rendering leaves every other simulator file byte for
  byte unchanged.
- `test_so101_train_apps.py` checks the `so101-record` and `so101-policy`
  templates without LeRobot or hardware: their settings parsing, the dataset
  resume and upload decisions, model revision resolution and camera matching,
  the stop sequence and calibration writes against a fake servo bus, that the
  two copies of `so101_common.py` stay identical, and that `wendy.json`, the
  `Dockerfile` and the README agree with the settings code.
- `test_elrobot_train_apps.py` does the same for `elrobot-record` and
  `elrobot-policy`, and also checks the 8-joint motor map of the vendored
  `elrobot_arms.py` and its license header, the calibration file checks
  against the URDF ranges, the calibration page's range capture and homing
  math (it must never write to the servos), which registers connecting writes
  with and without `ELROBOT_WRITE_CALIBRATION` and `ELROBOT_CONFIGURE_SERVOS`,
  the checks before torque goes on, that the shared files stay identical, and
  that each README names every register the app can write.

The checks read Git-tracked files and non-ignored untracked files, so ignored
build output such as `node_modules/`, `.build/`, and `target/` is never scanned.

Add a test when introducing a selectable template, a shared source copy, or a
repository rule that would otherwise drift silently.
