# go2-foxglove

Stream a Unitree Go2's front camera and validated robot data into Foxglove over
one WebSocket. This template publishes only canonical channels; it does not
forward the Go2's raw ROS/DDS graph.

That distinction matters. A generic ROS bridge can discover multiple Go2
publishers for `/lf/lowstate` or `/lf/sportmodestate` whose advertised schema or
encoding differs. Foxglove then reports duplicate-channel or unsupported-
encoding errors. This adapter instead subscribes to explicit `unitree_go` types,
accepts the `rt/*` and `rt/lf/*` firmware variants, selects one live source, and
publishes one stable channel for each concept.

```text
Go2 controller 192.168.123.161
  ├─ unitree_go LowState/SportModeState ─┐
  ├─ WebRTC video → camera watchdog ─────┼─ canonical adapter ─ Foxglove WS :8765
  ├─ MID-360 PointCloud2 ────────────────┤
  └─ UWB (optional) ─────────────────────┘

Canonical channels:
  /go2/camera   foxglove.CompressedImage
  /go2/joints   foxglove.JointState
  /go2/pose     foxglove.PoseInFrame
  /tf           foxglove.FrameTransform
  /go2/points   foxglove.PointCloud
  /go2/state    versioned JSON (fixed keys and 12-joint order)
  /go2/uwb      versioned JSON
  /go2/health   source age, selected topics, counters, and errors
```

The camera runs in a separate supervised service and forwards validated JPEGs to
the bridge over device localhost. Camera or LiDAR failures do not take down state
publishing. State samples containing partial arrays or non-finite values are
rejected rather than emitted as apparently healthy data. `/go2/state` is
published at 20 Hz only while LowState is fresh (500 ms by default).

## Deploy

```bash
wendy init --template go2-foxglove --language python --app-id go2viz
cd go2viz
wendy run --device <go2>.local --detach
```

The default controller address is `192.168.123.161`. At startup the adapter asks
the kernel which local IPv4 address routes to the controller, then pins both DDS
participants to that exact address. Set `GO2_DDS_ADDRESS` in the generated
Dockerfile only when route-based selection is not correct for a custom topology.

The Foxglove WebSocket and diagnostics bind to the Go2 dev PC's interfaces so
Foxglove on the same trusted LAN can reach them. Do not place the Go2 on an
untrusted network or expose these ports directly to the internet. Connect
Foxglove to the dev PC's LAN address, and query diagnostics at the same address:

```bash
ws://<go2-dev-pc-lan-ip>:8765
curl -fsS http://<go2-dev-pc-lan-ip>:8766/healthz | jq
```

Then:

1. Open Foxglove.
2. Choose **Open connection → Foxglove WebSocket**.
3. Connect to `ws://<go2-dev-pc-lan-ip>:8765`.
4. Import `foxglove-layout.json` from the generated project.
5. Inspect `/go2/health` or the HTTP health endpoint above.

Wendy Cloud tunneling works for the HTTP diagnostics endpoint on the test Go2,
but the Foxglove WebSocket handshake through the tunnel still needs a Wendy
transport fix or additional verification. Until that gate passes, use the
trusted-LAN connection above rather than assuming a remote WebSocket tunnel will
work.

## Healthy result

`/healthz` is a liveness and data-integrity report, not merely a process check.
The top-level `ok` becomes true only when DDS initialized, LowState is fresh, and
the camera produced a recent valid JPEG. SportModeState is reported separately
because some Go2 modes do not publish it. LiDAR and UWB are optional and also have
independent freshness/errors.

Before treating a robot as commissioned, verify all of the following:

- top-level health remains `true` for 10 minutes;
- `state.lowstate_received`, `state.published`, and `camera.frames` continuously
  increase;
- `state.lowstate_topic` remains on one `rt/lf/lowstate` or `rt/lowstate` source
  rather than oscillating;
- `/go2/joints` always contains the same 12 names in the same order;
- the Foxglove image panel advances rather than showing a frozen last frame;
- Foxglove's Problems panel contains no duplicate-schema or unsupported-encoding
  errors;
- restarting the app restores state and camera without manual cleanup;
- interrupting controller connectivity makes health fail closed, and restoring it
  recovers the stream.

## Camera behavior

The template pins `unitree-webrtc-connect==2.1.2`, decodes the Go2 WebRTC track,
rate-limits it to 15 FPS, and forwards JPEGs with a session ID, monotonic frame
number, and capture timestamp. The bridge rejects malformed, oversized,
out-of-order, or clock-skewed frames. The camera reconnects after track stalls and
reports its last error and restart count at `http://<device>:8768/healthz`.

The Go2 permits one WebRTC client. Close the Unitree phone app and stop any other
WebRTC camera/audio service before commissioning this feed. If the camera service
cannot obtain the slot, robot state continues and `/go2/health` fails closed with
a stale camera instead of presenting a frozen frame as healthy.

## Firmware topic variants

The adapter subscribes to both of these candidate sets by default:

```text
LowState:       rt/lf/lowstate, rt/lowstate
SportModeState: rt/lf/sportmodestate, rt/sportmodestate
```

Whichever alias produces a valid sample first stays active until it becomes
stale. This tolerates firmware variation without combining duplicate streams.
Override `LOWSTATE_TOPICS` or `SPORT_TOPICS` as comma-separated environment
variables only after confirming the robot's graph.

## Remaining hardware gate

The implementation has unit and local protocol tests, but a specific Go2 must
still pass the commissioning checks above before this template is treated as
production-ready. Keep any rollout PR in draft until both ARM64 images, WebRTC
slot behavior, exact DDS topics, restart recovery, and the 10-minute soak have all
passed on the target Go2.
