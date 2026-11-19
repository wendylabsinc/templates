# Third-party code and data

## `elrobot_arms.py`: the ElRobot LeRobot classes

`elrobot_arms.py` is vendored from the community LeRobot integration for
ElRobot, [dreamerarun/elrobot_teleoperation](https://github.com/dreamerarun/elrobot_teleoperation)
at commit `2fe70041bb33775a574304c759602f218fb97222`. That repository is
licensed under the Apache License 2.0 (the text is in
[licenses/elrobot_teleoperation.LICENSE](licenses/elrobot_teleoperation.LICENSE)).
Its code derives from LeRobot's SO-101 classes and keeps their
"Copyright 2025 The HuggingFace Inc. team" header, which `elrobot_arms.py`
keeps too. Neither repository has a NOTICE file.

The module combines four upstream files:

- `elrobot_follower/config_elrobot_follower.py`
- `elrobot_follower/elrobot_follower.py`
- `elrobot_leader/config_elrobot_leader.py`
- `elrobot_leader/elrobot_leader.py`

Changes, made so the classes run on LeRobot 0.6.1 (the integration targets an
older LeRobot and is not pinned to a version):

- The relative imports of the LeRobot source tree (`..config`, `..robot`,
  `..utils`, `..teleoperator`) import from the installed `lerobot` package
  (`lerobot.robots`, `lerobot.robots.utils`, `lerobot.teleoperators`).
- Both configs gain `num_read_retries` (default 2), passed to `sync_read`, as
  LeRobot 0.6.1's SO follower and leader do.
- The leader config gains `use_degrees` (default `False`, the upstream
  behaviour), so the leader can report the same units as the follower.
  Upstream, the leader always reports -100 to 100, so a follower with
  `use_degrees=True` would receive the leader's -100..100 values as degrees.
- The follower reads its cameras with `read_latest()`, as LeRobot 0.6.1's SO
  follower does, instead of `async_read()`.
- The leader's `disconnect()` raises `DeviceNotConnectedError` when it is not
  connected; upstream created the error without raising it.

Nothing else changed. The registered type names stay `elrobot_follower` and
`elrobot_leader`, so datasets record `robot_type: elrobot_follower`.

The templates do not use the vendored `calibrate()` (it asks on a terminal)
or, by default, `configure()` (it writes PID gains and gripper limits to the
servos' EEPROM). `elrobot_common.wendy_arm()` subclasses both classes and
replaces them; the README explains what the apps write instead.

## Joint limits from norma-core

`elrobot_common.URDF_LIMITS_DEG` holds the follower's joint limits from
norma-core's URDF,
[`hardware/elrobot/simulation/elrobot_follower.urdf`](https://github.com/norma-core/norma-core/blob/0d17bb300d712eeb5437d41ddb95ce380b5384e6/hardware/elrobot/simulation/elrobot_follower.urdf)
at commit `0d17bb300d712eeb5437d41ddb95ce380b5384e6` (the ElRobot hardware is
licensed under the Apache License 2.0; the norma-core repository as a whole
under MIT). Only the numbers are used: joints `rev_motor_01` to
`rev_motor_08` map to servo IDs 1 to 8, and every joint's velocity limit is
4.71 rad/s. No norma-core code is copied.
