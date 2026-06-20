# go2-initial-test

A **pre-hackathon hardware self-test** for the Unitree Go2 on WendyOS. Every
hardware interface a team might use gets its own tiny test app; a **dashboard
UI** polls them all and shows a live **green / red / pending** go/no-go board so
the Wendy team can confirm each Go2 before the event.

```
go2-initial-test/
├── wendy.json     ← multi-service app group (one service per test + ui)
├── ui/            ← dashboard: pass/fail grid + "Run walk test" button   [BUILT]
├── gpu/           ← GPU/CUDA test + Jetson deep-dive (cores/TensorRT/nvpmodel) [BUILT]
├── lowstate/      ← IMU + foot + battery + joints + odometry + remote    [BUILT]
│                    (one SDK process: rt/lowstate + sportmodestate + wirelesscontroller)
├── camera/        ← RGB camera (WebRTC) — capture 1 frame               [BUILT]
├── lidar/         ← LiDAR PointCloud2 (DDS) — 1 cloud, point count      [BUILT]
├── mic/           ← microphone (ALSA) — record 3 s, report level        [BUILT]
├── speaker/       ← speaker (DDS /audioreceiver) — play clip (manual ✔) [BUILT]
├── motion/        ← SportClient: walk + posture/gait + obstacle-avoid    [BUILT]
│                    + acrobatics (⚠ hard-gated, double-confirm) — all MANUAL
├── vui/           ← VUI head light + volume (VuiClient) — MANUAL        [BUILT]
├── storage/       ← persist volume write/read + free space              [BUILT]
├── cloud/         ← internet + Wendy Cloud reachability                 [BUILT]
└── extras/        ← ultrasonic + gimbal (N/A — no known API)            [BUILT]
```

## Status contract (how the UI aggregates)

Every test service is a small FastAPI app that:
- `GET /status` → `{ "results": [ { "interface", "status", "detail", "data" }, … ] }`
  `status` ∈ `pass | fail | pending | manual | na`. Most services return one
  result; `lowstate` returns several (imu/foot/battery/joints/odometry/remote/uwb),
  `cloud` returns two (internet/cloud), `extras` returns two (ultrasonic/gimbal).
- `POST /run` → re-run the test (the UI's per-tile "re-run", and the motion
  "Run walk test" button).

The `ui` service fans out to each test on `127.0.0.1:<port>` (all services use
`network: host`), merges every `results[]`, and renders one tile per interface.
Interfaces with no live result show **pending** so an undeployed/unreachable test
is visible, not silently missing.

**Port map:** ui `{{.UI_PORT}}` · gpu 3610 · lowstate 3611 · camera 3612 ·
lidar 3613 · mic 3614 · speaker 3615 · motion 3616 · cloud 3617 · extras 3618 ·
storage 3619 · vui 3620 · bt 3621 · btscan 3622.

## Per-interface access spec (the build map)

Derived from the existing Go2 demos (`/demos/go2-*`) — this is how each test
reaches the hardware.

| Interface | Access | Topic / API | Entitlements | Notes |
|---|---|---|---|---|
| **GPU/CUDA** | `torch` | `torch.cuda` + tiny matmul | `gpu`, `network` | base image must match the Go2's JetPack |
| **IMU** | DDS `unitree_sdk2py` | `rt/lowstate` → `imu_state.rpy` | `network` | shared `lowstate` service |
| **Foot contact** | DDS | `rt/lowstate` → `foot_force[4]` (N) | `network` | contact if force > ~50 N |
| **Battery** | DDS | `rt/lowstate` → `bms_state.soc`, `power_v` | `network` | low < 15%, crit < 5% |
| **Camera** | WebRTC | `unitree_webrtc_connect` video track → 1 frame | `network` | **only one WebRTC client allowed** |
| **LiDAR** | DDS | `rt/utlidar/cloud_deskewed` (PointCloud2) | `network` | **QoS BEST_EFFORT**; CycloneDDS from source |
| **Microphone** | ALSA | `sounddevice` capture from Jetson mic | `audio`, `network` | uses local mic to avoid WebRTC contention |
| **Speaker** | DDS | `rt/audioreceiver` (G.711 µ-law) | `network` | manual "did you hear it?" confirm (plays via DDS, so no `audio` entitlement) |
| **Motion** | SDK2 | `SportClient.Move/StopMove` | `network` | **MANUAL trigger**; velocity watchdog auto-stops |
| **Cloud** | HTTP | push JSON → confirm receipt | `network` | — |
| **Ultrasonic** | ⚠ unknown | no DDS topic / SDK method found | `network` | **probe + flag unverified** |
| **Body LEDs** | ⚠ unknown | no LED API found in demos | `network` | **probe + flag unverified** |
| **Camera gimbal** | N/A | fixed forward camera on standard Go2 | — | reported `na` |

### Networking (required for the DDS/SDK/WebRTC tests)
- DDS binds by **IP address**, not interface name: SDK services set
  `CYCLONEDDS_URI` to `<NetworkInterface address="{{.GO2_DDS_ADDRESS}}">` then
  call `ChannelFactoryInitialize(0)`; the `cyclonedds.xml` services template the
  same address. The Go2 Orin is multi-homed (eth1 carries both `192.168.100.x`
  and `192.168.123.x`), so a name like `eth0`/`eth1` is ambiguous and DDS can
  advertise the wrong subnet — binding by IP fixes it (same fix as `go2-rc`).
- **`GO2_DDS_ADDRESS`** = *this device's own* IP on the robot LAN (~`192.168.123.18`).
  **`GO2_IP`** = the *robot controller's* IP (~`192.168.123.161`), the WebRTC target.
  They're different and both needed.
- LiDAR/SDK Dockerfiles build **CycloneDDS 0.10.5 + unitree_sdk2_python from
  source** (no arm64 wheels) — copy the steps from `/demos/go2-motion` and
  `/demos/go2-camera` Dockerfiles.

## Build status & method

**All 14 services / 25 tiles are built** (the dashboard + every test). They have NOT yet
been verified on a live Go2 — DDS/WebRTC/SDK code needs a real robot to exercise.
Each Go2-specific service was adapted from the **proven demo code** rather than
written from scratch:
- motion ← `/demos/go2-motion/go2_controller.py`
- camera ← `/demos/go2-camera/main.py`
- lidar  ← `/demos/go2-Watchtower/go2_lidar_filter.py`, `go2-camera/perception.py`
- lowstate ← `/demos/go2-motion/go2_controller.py` (LowState subscriber)
- mic/speaker ← `/demos/go2-Watchtower/go2_*_bridge.py`, `/demos/go2-camera/audio.py`

## Deploy

```sh
# render the template, then from the rendered dir:
wendy --device <go2>.local run --detach            # whole board
# open the dashboard:
wendy utils open-browser http://<go2>.local:{{.UI_PORT}}
```

## Deploy footprint, ports & isolation (read before deploying the full board)
- **Disk / RAM:** ~14 containers run at once. The GPU image (`dustynv/pytorch:…`)
  is ~7–10 GB alone; with 5 source-built CycloneDDS/SDK services + camera
  (opencv/aiortc) the full pull is large, and torch+opencv+5 DDS participants
  resident can pressure an **8 GB Orin toward OOM** — an OOM-killed tile reads as a
  *hardware* failure. **Pre-pull images before the event**, and consider deploying
  `gpu`/`camera` on demand rather than always-resident. (Collapsing the 5 CycloneDDS
  builds into one shared base image cuts disk + build time — see follow-ups.)
- **arm64 only:** the GPU service's `dustynv/*` base is **arm64-only**, so `wendy run`
  from an x86 laptop fails the GPU *build*, not just at runtime. Build on/for arm64.
- **Host ports:** every service uses `network: host` and binds a **fixed host port**
  on the dog — ui `{{.UI_PORT}}`, backends **3610–3622**. These must be FREE; if the
  robot's own stack or another app group already holds one, that tile fails to bind.
  Only `UI_PORT` is overridable today (relocating a backend port = editing the
  service's Dockerfile + `ui/main.py`).
- **Bluetooth isolation:** `btscan` is the only service that needs the `bluetooth`
  entitlement; if it destabilizes the group deploy on your agent build, just delete
  the `btscan` block from `wendy.json` — the passive **Bluetooth (radio)** tile still
  covers presence. The `ui` dashboard has **no `dependsOn`**, so a service that fails
  to start can't block the board from coming up.
- **`wendy run` build is all-or-nothing:** a single service that fails to *build*
  aborts the whole group deploy — **nothing runs**, even the services that built. So
  every service must build cleanly; to iterate on a subset, temporarily remove the
  broken service(s) from `wendy.json`. (Verified on a real deploy.)
- **buildx cache race (host/CLI issue, not the template):** building all 14
  services concurrently shares one local buildx cache, and parallel layer-export
  can clobber its ingest dir → random `rename tmp file … no such file` failures on
  ~3 heavy services per run. Clearing `~/Library/Caches/wendy/buildx` only helps
  when builds fail *fast*; once they run to completion they all export at once and
  the race gets *worse*. **Reliable workaround — build the heavy services serially,
  then deploy the cached group:**
  ```sh
  for s in gpu lowstate vui motion storage bt; do
    wendy run --service "$s" --device <dev> -y --deploy
  done
  wendy run --device <dev> -y --detach   # all cache hits → the group deploys
  ```
  This also fixes the next item (it pushes one image at a time, not 14 at once).
- **Registry-push timeouts under concurrency (host/tunnel issue, not the template):**
  a full `wendy run` pushes all 14 images at once through one ephemeral mTLS registry
  tunnel; the ~10 GB `gpu` image dominates and concurrent pushes overwhelm the tunnel
  (`TLS handshake timeout`, buildkit `retrying in 4s` ×80+). The device itself stays
  healthy (plenty of disk) — it's the tunnel under ~10 concurrent pushers. The
  serialized deploy above avoids it. The one template-side lever is **image size**:
  the `gpu` service's `dustynv/pytorch` base (~10 GB) is the bulk; the test only needs
  torch + TensorRT, so a slimmer CUDA/torch `GPU_BASE_IMAGE` (matched to the dog's
  JetPack) would cut the push payload the most. (Left as-is here — swapping the base
  blind risks breaking the torch/CUDA test; tune it once you can verify on the unit.)

## Pre-event verification (needs a live Go2 — can't be settled by static review)
- **Build dedup (G1):** the 5 DDS/SDK services share a byte-identical
  ubuntu+CycloneDDS prefix, and `wendy run` builds with a shared local buildx cache
  (`--cache-from/--cache-to`), so on a clean first build CycloneDDS should compile
  **once** then the other four hit cache. Watch the first build: if you see five
  CycloneDDS compiles, a prefix drifted (they must stay character-identical).
- **Group blast radius (G2):** the board is one app group of 14 `network: host`
  containers. Deploy it once with a service **deliberately broken** (e.g. a bogus
  `GPU_BASE_IMAGE`, or the `bluetooth` entitlement unavailable) and confirm **the
  rest of the board still comes up**. `ui` has no `dependsOn`, but if the agent
  aborts the *group* when one container can't start, split the fragile services
  (`btscan`, maybe `gpu`) into a secondary group. This is the top pre-event check.
- **Motion stop:** validate on a stand / e-stop ready. The walk test now issues
  `StopMove` and **verifies body velocity reached ≈0** via `rt/sportmodestate`
  before reporting "stopped" (reports "no velocity feedback" if sportmodestate
  isn't flowing) — confirm that path on the real robot.
- **RAM/disk (G4):** pre-pull images; the CUDA image is ~7–10 GB and the full group
  is heavy on an 8 GB Orin (an OOM-killed tile reads as a hardware fail).

## Known risks for the hackathon
- **Ultrasonic** and **Body LEDs**: no working example exists — treat as
  unverified until confirmed with Unitree (DDS topic / I2C path).
- **WebRTC is single-client**: the camera test grabs the one slot; if the
  Unitree phone app is connected, camera will fail until it disconnects.
- **GPU**: Jetson often boots with few cores online; the test reports
  availability + a timing, not a FPS guarantee.
- **Bluetooth (two tiles):** `Bluetooth (radio)` confirms an adapter/dongle is
  *present* via sysfs — no `bluetooth` entitlement, so it always starts/reports.
  `BT scan/pair` (the `btscan` service) DOES use the `bluetooth` entitlement to run
  a real BlueZ BLE discovery (and optional connect/pair via `BT_PAIR_TARGET`); if
  that tile is stuck `pending`/unreachable, the `bluetooth` entitlement's
  dbus-proxy isn't being created (the failure we saw on the rc-car).
- **Acrobatics**: hard-gated — needs the UI double-confirm AND `ENABLE_ACROBATICS=1`
  on the motion service; never auto-runs. Physically dangerous (flip/jump).
