# lerobot-train image

`ghcr.io/wendylabsinc/lerobot-train:lerobot-0.6.1` is the image `wendy train`
runs on a rented Nebius GPU. It trains a [LeRobot](https://github.com/huggingface/lerobot)
0.6.1 policy (ACT or SmolVLA) on a LeRobot dataset from the Hugging Face Hub,
and pushes the trained model back to the Hub.

The entrypoint wraps LeRobot's own `lerobot-train`. It adds four things:

- **Input checks before any GPU time is spent.** It resolves the dataset
  revision to a commit, checks that the dataset is a v3.x `LeRobotDataset`,
  and checks that `HF_TOKEN` can create the model repo.
- **Resume.** It continues from the newest complete checkpoint in the output
  directory, so a preempted job that restarts loses little work.
- **Machine-readable events** on stdout.
- **One-commit push** of the final model, with a model card, and the commit id.

The image is linux/amd64 only. CUDA 12.8 comes with the torch wheels, so the
host needs an NVIDIA driver >= 570 and the NVIDIA container runtime.

## Inputs

The entrypoint takes no arguments. Every input is an environment variable.
Empty counts as unset.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `WENDY_TRAIN_DATASET` | yes | | Hugging Face dataset repo id, `namespace/name`. |
| `WENDY_TRAIN_DATASET_REVISION` | no | `main` | Branch, tag or commit. Resolved to a commit at start; training uses that commit. |
| `WENDY_TRAIN_POLICY` | yes | | `act` or `smolvla`. |
| `WENDY_TRAIN_STEPS` | no | 20000 (act), 20000 (smolvla) | Training steps. |
| `WENDY_TRAIN_BATCH_SIZE` | no | 8 (act), 64 (smolvla) | Batch size. |
| `WENDY_TRAIN_PUSH` | no | `true` | `true` or `false`. Whether to push the model. |
| `WENDY_TRAIN_MODEL_REPO` | when pushing | | Model repo id to push to. It is created if missing. |
| `WENDY_TRAIN_OUTPUT_DIR` | no | `/workspace/output` | Checkpoints. If it holds one, the run resumes from the newest. |
| `WENDY_TRAIN_EXTRA_ARGS` | no | | JSON array of strings appended to `lerobot-train`'s arguments as is, with no shell parsing, e.g. `["--num_workers=8", "--wandb.enable=true"]`. |
| `HF_TOKEN` | when pushing, or for a private dataset | | Hugging Face token. It is never printed or written to the model card. |

Bad input fails at once with exit status 2 and an `error` event that names the
variable. All problems are reported together.

`WENDY_TRAIN_EXTRA_ARGS` may not set the flags the entrypoint owns. Use the
variable instead: `--dataset.repo_id`, `--dataset.revision`, `--policy.type`,
`--policy.path`, `--policy.pretrained_revision`, `--policy.push_to_hub`,
`--policy.repo_id`, `--save_checkpoint_to_hub`, `--steps`, `--batch_size`,
`--output_dir`, `--resume`, `--config_path`, `--save_checkpoint`,
`--job.target` and `--help`. Anything else goes through, and a flag given
there wins over the entrypoint's defaults (`--log_freq`, `--save_freq`,
`--num_workers`, `--rename_map`, `--wandb.*`, …). To log to Weights & Biases,
also pass `WANDB_API_KEY`; it is scrubbed from output like `HF_TOKEN`.

### Policies

- **`act`**: `--policy.type=act`, trained from scratch, LeRobot's default
  batch size of 8.
- **`smolvla`**: fine-tunes `lerobot/smolvla_base` at a pinned commit
  (`SMOLVLA_BASE_REVISION` in `entrypoint.py`), batch size 64, as LeRobot's
  SmolVLA guide does. `--policy.type=smolvla` alone would leave the
  vision-language backbone untrained. If your camera names differ from the
  base model's, map them with
  `WENDY_TRAIN_EXTRA_ARGS='["--rename_map={\"observation.images.front\": \"observation.images.camera1\"}"]'`.

## Output

stdout carries only machine-readable lines: `WENDY_TRAIN ` and one compact
JSON object with an `event` field. Everything human-readable goes to stderr:
`lerobot-train`'s own log, and the entrypoint's notes, which are prefixed
`[wendy-train]`.

```text
WENDY_TRAIN {"event":"start","dataset":"team/so101_pick","dataset_revision":"a1b2…","policy":"act","steps":20000,"lerobot_version":"0.6.1","resumed_from_step":0}
WENDY_TRAIN {"event":"progress","step":200,"total":20000,"loss":0.512}
…
WENDY_TRAIN {"event":"checkpoint","step":2000}
…
WENDY_TRAIN {"event":"pushing","model":"team/so101_pick-act"}
WENDY_TRAIN {"event":"done","model":"team/so101_pick-act","revision":"<40-hex commit>","local_path":"/workspace/output/train/checkpoints/020000/pretrained_model"}
```

| Event | Fields | When |
|---|---|---|
| `start` | `dataset`, `dataset_revision` (commit), `policy`, `steps`, `lerobot_version`, `resumed_from_step` | Inputs are valid, the dataset is resolved, the model repo is writable. |
| `progress` | `step`, `total`, optional `loss` | At least every 1% of the steps, or 30 s if steps are slower. `loss` is the latest training loss LeRobot logged, averaged over its log window. It is left out until the first one, and when it is NaN or infinite. |
| `checkpoint` | `step` | A checkpoint is completely on disk. Reported in order, after `progress` has reached its step. |
| `pushing` | `model` | The push starts. |
| `done` | `model`, `revision`, `local_path` | Finished; the only event before exit 0. `revision` is the 40-hex commit of the pushed model. Without a push, `model` and `revision` are `""`. `local_path` is the final `pretrained_model` directory. |
| `error` | `message` | The last event before a non-zero exit. |

Exit status: 0 after `done`; 2 for bad input; `lerobot-train`'s own status when
it fails (128 + signal number when it is killed, e.g. 137 for an out-of-memory
kill); 143 after SIGTERM; 1 for anything else.

`tests/event_contract.py` checks a captured stdout against this contract:
`python3 tests/event_contract.py run.out --steps 20000 --push true`.

## Checkpoints and resume

`lerobot-train` writes into `$WENDY_TRAIN_OUTPUT_DIR/train/`. It refuses to
start a fresh run in an existing directory, and the output directory is often
a mount point, so it gets its own subdirectory:

```text
$WENDY_TRAIN_OUTPUT_DIR/train/checkpoints/
  018000/{pretrained_model,training_state}/
  020000/{pretrained_model,training_state}/
  last -> 020000
```

- A checkpoint is written every 10% of the steps, but no more often than every
  1000 steps, and always after the last step. Only the newest two are kept,
  because a SmolVLA checkpoint is several GB.
- On start, the entrypoint picks the newest complete checkpoint. A step
  directory newer than `last` may have been half-written when the job stopped,
  so it is skipped. The run then continues with `lerobot-train --resume=true`
  on the dataset commit recorded in that checkpoint, even if the branch has
  moved since.
- If the newest checkpoint already holds all the steps, training is skipped
  and only the push runs. This covers a job stopped while pushing.
- The checkpoint must be for the same dataset and policy, and for no more
  steps than `WENDY_TRAIN_STEPS`. Otherwise the run fails without changing
  anything.
- A `train/` directory with no complete checkpoint is left over from a run
  stopped before its first checkpoint, and is deleted.

On SIGTERM, from a cancelled or preempted job, `lerobot-train` and its
dataloader workers are stopped, and killed after 20 s if they are still
running. The run then reports an `error` and exits 143.

## The pushed model

One commit to `WENDY_TRAIN_MODEL_REPO` holds everything in the final
`pretrained_model` directory and a `README.md` model card. That directory
contains the policy config, `model.safetensors`, `train_config.json`, and the
pre/post-processor files with the normalization stats. The card names:

- the dataset and its commit
- the dataset format
- the policy, the steps and the batch size
- the base model and its commit (for SmolVLA)
- the image tag and the wendylabsinc/templates commit it was built from
- the LeRobot version
- any extra `lerobot-train` arguments

Load the model pinned to the reported commit:

```python
from lerobot.policies.act.modeling_act import ACTPolicy

policy = ACTPolicy.from_pretrained("team/so101_pick-act", revision="<revision from done>")
```

## Building and testing

CI (`.github/workflows/lerobot-train-image.yml`) runs on every pull request
that touches this folder. It runs the entrypoint tests, builds the image, and
runs a CPU smoke train: 3 ACT steps on `lerobot/svla_so101_pickplace`, then a
resume to 6 steps, with `WENDY_TRAIN_PUSH=false`. A push to main also
publishes `:lerobot-0.6.1` and `:lerobot-0.6.1-<commit>`.

The entrypoint tests need only Python 3.12. They use a stub `lerobot-train`
that writes LeRobot 0.6.1's log lines and checkpoint layout, and a fake
`huggingface_hub`:

```sh
cd images/lerobot-train
python3 -m unittest discover -s tests -v
```

With the `huggingface_hub` version from `uv.lock` installed, the tests also
check every Hub call against the real library's signatures.

### Changing the LeRobot version

1. Change the `lerobot[...]==` pin in `pyproject.toml`, then run
   `uvx --from uv==0.13.5 uv lock` in this folder.
2. Change the version in the Dockerfile (`IMAGE_TAG`, the
   `sh.wendy.lerobot.version` label), in `DEFAULT_IMAGE` in `entrypoint.py`,
   in `LEROBOT_VERSION` in the workflow, and in this file. Also change the
   `huggingface_hub` version in the workflow if the lock moved it.
   `PinsTest` fails until they all agree.
3. Check that LeRobot's log format and checkpoint layout haven't changed
   (`lerobot/scripts/lerobot_train.py`, `lerobot/utils/logging_utils.py`,
   `lerobot/common/train_utils.py`). The stub and the captured lines in
   `tests/test_entrypoint.py` copy them. Also check that the pinned
   `lerobot/smolvla_base` commit still loads.
