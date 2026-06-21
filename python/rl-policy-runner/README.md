# rl-policy-runner

Run a trained RL locomotion policy on a robot as a safe, managed WendyOS app — the
on-robot inference half of a sim-to-real pipeline. **It ships wired for the
pre-trained [Walk-These-Ways](https://github.com/Teddy-Liao/walk-these-ways-go2)
(WTW) Unitree Go2 gait policy** (the two TorchScript nets are baked into the image),
so it's runnable out of the box on a Go2 — no training required.

```
[runner]  rt/lowstate → WTW obs(70) → history(15) → adaptation_module → latent
   │       → body(cat(history,latent)) → 12 joint deltas
   │       → target_q = default+act·0.25 (hip·0.5) → rt/lowcmd PD @ 50 Hz
   │       (+ independent watchdog → damping stop, startup calibration ramp)
   └── HTTP control API: /start /stop /estop /status
[ui]      web dashboard: live status + Start / Stop / ■ E-STOP
```

## What it does with wendy

`wendy init python/rl-policy-runner` → set `GO2_IP`, `GO2_DDS_ADDRESS`, optional
`CMD_VX` → `wendy run` deploys both services as containers onto the Go2's onboard
Jetson (no reflash). The bundled WTW checkpoint runs immediately. Drive/inspect via
the web UI and `wendy device logs`.

## ⚠️ Safety — this drives a real quadruped

- Won't move until you deploy with **`ENABLE_POLICY=1` and `ENABLE_LOWCMD=1`** and
  press **Start**.
- **Requires the Go2's high-level `sport_mode` to be OFF** (low-level control is
  mutually exclusive with sport mode on EDU/EDU+).
- On Start it runs a **calibration ramp** to the default stance (no jerk), then the policy.
- An **independent `threading.Timer` watchdog** fires a synchronous **damping stop**
  if the loop stalls; the loop also damping-stops on stop/error/disconnect.
- **First runs on a stand / suspended, e-stop ready.**

## ✅ [VERIFY] before any free-standing run

The WTW Go2 convention is replicated from its deploy code, but four values are the
*standard* config and must be confirmed — a mismatch produces confident, wrong,
*moving* output. The runner **auto-checks #1–#2** at startup (it infers the latent
dim and validates the obs-history size against the actual `.jit` shapes — a mismatch
errors loudly instead of running); **#3–#4 you must check on a stand:**

1. **Obs size / latent dim** — `NUM_OBS=70`, `HISTORY_LEN=15`, latent inferred. Auto-checked.
2. **Net wiring** — `latent = adaptation_module(history)`, `body(cat(history, latent))`. Auto-checked.
3. **Joint permutation** (`JOINT_IDXS = [3,4,5,0,1,2,9,10,11,6,7,8]`, policy↔SDK) — derived,
   not copied from `cheetah_state_estimator.py`. **Twitch-test one joint** at low gains
   on a stand and confirm the expected motor moves before trusting all 12.
4. **Projected-gravity sign** — should read `[0,0,-1]` upright. Confirm on the dashboard
   (hold the dog level) before enabling motion; a flipped sign will tip the robot.

Gains/scales baked in (from WTW go2_config): `KP=25`, `KD=0.6`, `ACTION_SCALE=0.25`,
`hip_scale_reduction=0.5`, `dof_vel_scale=0.05`, default trot (freq 3.0, phase 0.5).

## Deploy

```sh
wendy init python/rl-policy-runner       # set GO2_IP, GO2_DDS_ADDRESS, CMD_VX
cd <app-id>
wendy run                                 # build + deploy to the Go2
```

Open `http://<device>:<WEB_PORT>`, confirm *policy loaded* + the startup self-check
detail (obs/latent sizes ok) + obs values changing (dog on a stand). To allow motion,
deploy with `ENABLE_POLICY=1 ENABLE_LOWCMD=1` (set in `runner/Dockerfile` or as deploy
env), turn off sport_mode, do the joint twitch-test, then Start. `CMD_VX=0` trots in
place; set `0.3` to walk forward.

## Variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `APP_ID` | — | Application identifier |
| `WEB_PORT` | `8080` | Dashboard port |
| `GO2_IP` | `192.168.123.161` | Robot controller IP |
| `GO2_DDS_ADDRESS` | `192.168.123.18` | This device's robot-LAN IP DDS binds to |
| `CMD_VX` | `0.0` | Forward walk speed (m/s); 0 = trot in place |

Non-templated env (set in `runner/Dockerfile` or per-deploy): `ENABLE_POLICY`/
`ENABLE_LOWCMD` (default 0), `KP`/`KD`/`STOP_KD`, `STEP_FREQ`, `FOOTSWING`,
`CMD_VY`/`CMD_VYAW`, `NUM_OBS`/`HISTORY_LEN`.

## To target a different policy

Edit `runner/controller.py` (obs construction, joint order, gains, default pose) and
swap the checkpoint download in `runner/Dockerfile`. For an Isaac Lab joint-target
policy, the prior commit (`a8a35bb`/`901f649` on this branch) has that adapter variant.

## Notes & attribution

- Inference uses **CPU torch** (the WTW nets are small MLPs; fine at 50 Hz). The image
  is large (torch) — acceptable for a test deployment.
- The bundled checkpoint and the deploy convention come from
  **Teddy-Liao/walk-these-ways-go2** (MIT), a Go2 port of
  **Improbable-AI/walk-these-ways** (Margolis & Agrawal). Credit/keep their license.
- The `runner` image builds CycloneDDS 0.10.5 + unitree_sdk2_python from source; build
  for `linux/arm64` on the Go2 Orin (wendy multibuild handles it).
