# rl-policy-runner

Deploy a trained reinforcement-learning policy onto a robot and run it as a safe,
managed WendyOS app — the on-robot inference half of a sim-to-real pipeline. **This
template is wired for the NVIDIA Isaac Lab Unitree Go2 velocity locomotion policy**
(`Isaac-Velocity-Flat-Unitree-Go2-v0`): it builds the exact Isaac-Lab observation,
runs your exported ONNX policy at 50 Hz, and applies joint-position targets to the
Go2 over `rt/lowcmd` — behind an independent safety watchdog and an ENABLE gate.

```
[runner]  rt/lowstate → Isaac-Lab obs(48) → policy → target_q = default+act*0.25
   │       → rt/lowcmd PD @ 50 Hz        (+ independent watchdog → damping stop)
   └── HTTP control API: /start /stop /estop /status
[ui]      web dashboard: live status + Start / Stop / ■ E-STOP
```

## What it does with wendy

`wendy init python/rl-policy-runner` → fill vars → edit nothing if your policy is a
stock Isaac Lab Go2 velocity policy → `wendy run` deploys both services as containers
onto the Go2's onboard Jetson (no reflash) → drive/inspect via the web UI and
`wendy device logs`. Swap the policy by changing `POLICY_URL` and `wendy run` again.

## ⚠️ Safety — this drives a real quadruped

- The policy will not move the robot until you deploy with **`ENABLE_POLICY=1`**,
  and (for `lowcmd`) **`ENABLE_LOWCMD=1`**, and press **Start**.
- `lowcmd` mode **requires the Go2's high-level `sport_mode` service to be OFF**
  (low- and high-level control are mutually exclusive on EDU/EDU+).
- An **independent `threading.Timer` watchdog** fires a synchronous **damping stop**
  if the loop stalls or isn't renewed within `WATCHDOG_S`.
- Joint targets are clamped to `±JOINT_DELTA_MAX` from the default pose every tick.
- The loop always issues a damping stop on stop/error/disconnect.
- **First runs on a stand / suspended, with a physical e-stop ready.**

## ✅ [VERIFY] before any free-standing run

The Isaac Lab convention is baked into `runner/controller.py`, but a few values
*must* be confirmed against **your** exported `env.yaml` and the actual robot — a
mismatch produces confident, wrong, *moving* output:

1. **PD gains** — `KP=25.0 / KD=0.5` (upstream IsaacLab). If your policy came from
   Unitree's `unitree_rl_lab`, it's likely **40.0 / 1.0**. Set `KP`/`KD` to match the
   repo that produced your checkpoint.
2. **Obs scaling** — the adapter feeds **raw SI** (Isaac Lab adds no legged_gym-style
   scales). Grep your `env.yaml` obs terms for any `scale:` and match if present.
3. **Joint order** — the SDK↔Isaac permutation (`ISAAC_FROM_SDK`/`SDK_FROM_ISAAC`) is
   derived, not copied. **Twitch-test one joint** (low gains, on a stand) and confirm
   the expected SDK index moves before trusting all 12.
4. **`base_lin_vel`** — the flat 48-dim obs includes base linear velocity, which is
   **privileged in sim and not available from `rt/lowstate`** (and `rt/sportmodestate`
   is silent in low-level mode). The adapter feeds **zeros** by default
   (`_base_lin_vel`). For real deployment, prefer a **blind 45-dim policy** (set
   `USE_BASE_LIN_VEL=0`, `OBS_DIM=45`) or wire a state estimator into `_base_lin_vel`.

## Deploy

```sh
wendy init python/rl-policy-runner      # scaffold; set POLICY_URL, GO2_IP, GO2_DDS_ADDRESS
cd <app-id>
# confirm the [VERIFY] items above match your policy, then:
wendy run                                # build + deploy to the Go2
```

Open `http://<device>:<WEB_PORT>`, confirm *policy loaded* + obs values changing
(dog on a stand). To allow motion, deploy with `ENABLE_POLICY=1 ENABLE_LOWCMD=1`
(set in `runner/Dockerfile` or as deploy env) and turn off sport_mode, then Start.
Set the walk command via `CMD_VX` / `CMD_VY` / `CMD_VYAW` (default 0 = stand/in-place).

## Variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `APP_ID` | — | Application identifier |
| `WEB_PORT` | `8080` | Dashboard port |
| `POLICY_URL` | — | Exported ONNX policy URL (or mount at `/policy/policy.onnx`) |
| `OBS_DIM` / `ACT_DIM` | `48` / `12` | Isaac Lab flat Go2 (use 45 for a blind policy) |
| `CONTROL_HZ` | `50` | Isaac Lab control rate |
| `CONTROL_MODE` | `lowcmd` | `lowcmd` (joint targets) or `velocity` (SportClient) |
| `ROBOT` | `go2` | Target robot |
| `GO2_IP` | `192.168.123.161` | Robot controller IP |
| `GO2_DDS_ADDRESS` | `192.168.123.18` | This device's robot-LAN IP DDS binds to |

Non-templated tuning/safety env (set in `runner/Dockerfile` or per-deploy):
`ENABLE_POLICY`/`ENABLE_LOWCMD` (default 0), `KP`/`KD`/`STOP_KD`, `ACTION_SCALE`
(0.25), `JOINT_DELTA_MAX` (1.2), `USE_BASE_LIN_VEL`, `CMD_VX/VY/VYAW`, `WATCHDOG_S`,
`MAX_VX/VY/VYAW` (velocity mode).

## To adapt to a different policy

Edit the named constants + the two adapters in `runner/controller.py`:
`DEFAULT_POSE_ISAAC`, `ISAAC_FROM_SDK`/`SDK_FROM_ISAAC`, `build_observation`,
`apply_action`. For a velocity-command policy, set `CONTROL_MODE=velocity` and
interpret the action as `[vx, vy, vyaw]` (SportClient, sport_mode stays on).

## Notes

- Inference is **CPU ONNX Runtime** (fine for a locomotion MLP at 50 Hz).
- The `runner` image builds **CycloneDDS 0.10.5 + unitree_sdk2_python from source**;
  build for the device arch (`linux/arm64` on the Go2 Orin — wendy multibuild handles it).
- Convention sourced from isaac-sim/IsaacLab (`UNITREE_GO2_CFG`, velocity env cfg) and
  the Unitree SDK LegID layout; see `runner/controller.py` comments.
