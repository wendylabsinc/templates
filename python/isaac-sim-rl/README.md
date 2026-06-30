# {{.APP_ID}}

Isaac Sim / IsaacLab reinforcement-learning template for Wendy devices.

The template starts from a robot profile, then resolves that profile to an
IsaacLab task id. Set `WENDY_TRAIN_TASK` directly when you have a custom task
for another Unitree robot or a non-locomotion workflow.

## What this gives you

- Isaac Sim image: `{{.ISAAC_SIM_IMAGE}}`
- IsaacLab ref: `{{.ISAACLAB_REF}}`
- RSL-RL trainer installed during image build
- GPU, host networking, and persistent `/logs`
- robot profile: `{{.ROBOT_PROFILE}}`
- training task: `{{.TASK_ID}}`
- automatic resume from the newest `/logs/rsl_rl/<experiment>/*/model_*.pt`
- a local task registration hook under `source/local_isaac_tasks`

## Run

```bash
wendy run .
```

The app writes checkpoints, TensorBoard events, videos, and launcher markers
under `/logs`. On a Wendy device that path is persistent because `wendy.json`
declares a persist entitlement.

## Common overrides

```bash
WENDY_ROBOT_PROFILE=unitree-go2-flat \
WENDY_TRAIN_TASK=auto \
WENDY_EXPERIMENT_NAME=auto \
WENDY_NUM_ENVS=1024 \
WENDY_MAX_ITERATIONS=20000 \
wendy run .
```

Use a custom Unitree task:

```bash
WENDY_ROBOT_PROFILE=custom-unitree \
WENDY_TRAIN_TASK=My-Unitree-Task-v0 \
WENDY_EXPERIMENT_NAME=my_unitree_task \
wendy run .
```

Disable video if startup or rendering is the bottleneck:

```bash
WENDY_RECORD_VIDEO=0 wendy run .
```

Start fresh instead of resuming:

```bash
WENDY_AUTO_RESUME=0 wendy run .
```

Pass trainer flags after `--`:

```bash
wendy run . -- --seed 7
```

## Add your own robot task

1. Put task config code under `source/local_isaac_tasks/local_isaac_tasks/`.
2. Register a Gymnasium task id in `source/local_isaac_tasks/local_isaac_tasks/__init__.py`.
3. Set `WENDY_TRAIN_TASK` to that id.
4. Keep the policy contract stable before real-world transfer: observation
   order, action order, joint names, scaling, control rate, and safety limits.

This is not limited to walking. Walking/velocity locomotion is only the easiest
default because IsaacLab already ships those Unitree Go2 tasks. Manipulation,
navigation, imitation, or whole-body tasks belong in `source/local_isaac_tasks`
with their own observations, actions, rewards, and termination rules.

## Sim-to-real checklist

- Export the trained policy from the RSL-RL run directory.
- Save the exact observation and action contract beside the checkpoint.
- Verify joint order and units against the real robot SDK before sending torque
  or position commands.
- Add a low-speed hardware smoke test before full policy rollout.
- Keep a manual stop path active during the first real-world run.
