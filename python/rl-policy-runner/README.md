# rl-policy-runner

Deploy a trained reinforcement-learning policy onto a robot and run it as a safe,
managed WendyOS app. This is the **on-robot inference counterpart** to a sim-to-real
training pipeline (e.g. Isaac Lab / MuJoCo): you train a policy on a GPU box, then
`wendy run` this template onto the robot's onboard computer to actually drive it.

```
[runner]  loads ONNX policy → reads robot DDS state → policy(obs) → clamp → act @ Hz
   │                                                   (+ independent safety watchdog)
   └── HTTP control API (start/stop/estop/status)
[ui]      web dashboard: live status + Start / Stop / ■ E-STOP
```

## What it does with wendy

- **`wendy init python/rl-policy-runner`** — scaffold the project; set the policy
  URL, obs/action dims, control rate, robot IP/DDS address, control mode.
- **`wendy run`** — builds the images and deploys them as **containers onto the
  robot's onboard computer** (no OS reflash) with least-privilege entitlements.
- On-device, `runner` runs the control loop over Unitree DDS; `ui` serves the
  dashboard on `WEB_PORT`. Inspect with `wendy device logs`, control via the web UI.
- **Update the policy** = change `POLICY_URL` and `wendy run` again.

wendy handles packaging, deploy, lifecycle, entitlements, and observability — it does
**not** make an incorrect or obs-mismatched policy work (see below).

## ⚠️ Safety (this drives a real robot)

- The policy **will not move the robot** until you both deploy with `ENABLE_POLICY=1`
  **and** press Start. Default is disabled.
- An **independent `threading.Timer` watchdog** (its own OS thread) fires a
  *synchronous* stop if the control loop stalls or isn't renewed within `WATCHDOG_S`.
- Actions are **clamped** every tick (`MAX_VX/MAX_VY/MAX_VYAW` for velocity mode).
- The loop **always stops the robot** on stop, error, or disconnect (`try/finally`).
- **Always test on a stand / suspended, with a physical e-stop ready.**

## You must edit two adapters to match your policy

The policy only behaves correctly if its observation/action layout matches how it was
trained. Edit these in `runner/controller.py`:

- **`build_observation()`** — which DDS fields → obs vector, in the exact order and
  scaling your policy was trained on. The default is an Isaac-Lab-style Go2 layout and
  is almost certainly not byte-for-byte correct for your policy.
- **`apply_action()`** — how the policy output maps to robot commands.

**Control modes:**
- `velocity` (default, safe) — high-level `SportClient`; interprets the first 3 outputs
  as `[vx, vy, vyaw]`. Works for high-level command policies. `ACT_DIM=3`.
- `lowcmd` (advanced) — direct joint control for joint-target policies (e.g. Isaac Lab
  locomotion). Ships as a **stub you complete** (per-joint kp/kd + default pose),
  requires `ENABLE_LOWCMD=1`, and **needs sport_mode turned OFF** on the robot. Verify
  on a stand.

## Deploy

```sh
wendy init python/rl-policy-runner     # scaffold into ./<app-id>/
cd <app-id>
# edit runner/controller.py adapters to match your policy, then:
wendy run                               # build + deploy to the connected robot
```

Open **`http://<device>:<WEB_PORT>`**, confirm status, then Start (with the dog on a
stand). To actually enable driving, deploy the runner with `ENABLE_POLICY=1` (set it
in `runner/Dockerfile` or as a deploy env).

## Variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `APP_ID` | — | Application identifier |
| `WEB_PORT` | `8080` | Dashboard port |
| `POLICY_URL` | — | ONNX policy URL (or mount one at `/policy/policy.onnx`) |
| `OBS_DIM` / `ACT_DIM` | `48` / `12` | Observation / action vector sizes (match training) |
| `CONTROL_HZ` | `50` | Control-loop rate |
| `CONTROL_MODE` | `velocity` | `velocity` (SportClient) or `lowcmd` (joint control) |
| `ROBOT` | `go2` | Target robot for the default adapters |
| `GO2_IP` | `192.168.123.161` | Robot controller IP |
| `GO2_DDS_ADDRESS` | `192.168.123.18` | This device's robot-LAN IP that DDS binds to |

Non-templated safety/runtime envs (set in `runner/Dockerfile` or per-deploy):
`ENABLE_POLICY` (default 0), `ENABLE_LOWCMD` (0), `MAX_VX/MAX_VY/MAX_VYAW`,
`WATCHDOG_S`, `ACTION_SCALE`.

## Notes

- Inference is **CPU ONNX Runtime** by default — fine for small locomotion MLPs at
  50 Hz. For larger policies on a GPU device, add a `gpu` entitlement and an ONNX
  Runtime GPU/TensorRT provider.
- The `runner` image builds **CycloneDDS + unitree_sdk2_python from source**; build for
  the device arch (`linux/arm64` on a Go2 Orin — wendy multibuild handles this).
- On the Go2 EDU/EDU+, low-level control requires the high-level `sport_mode` service to
  be OFF; the two are mutually exclusive.
