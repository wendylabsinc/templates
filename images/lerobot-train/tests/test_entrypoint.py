"""Tests of the lerobot-train image's entrypoint.

They need no GPU, no container and no network. Most run entrypoint.py as a
process, as the image does, with:

- tests/stub_bin/lerobot-train first on PATH: a stand-in that writes LeRobot
  0.6.1's log lines and checkpoint layout without training anything;
- tests/fake_site first on PYTHONPATH: a fake huggingface_hub that keeps the
  Hub's state in a JSON file, and lerobot's dist-info for its version.

HubSignatureTest checks the entrypoint's Hub calls against the real
huggingface_hub when it is installed (CI installs the version uv.lock pins).

Run from images/lerobot-train:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
IMAGE_DIR = HERE.parent
REPO_ROOT = IMAGE_DIR.parent.parent
ENTRYPOINT = IMAGE_DIR / "entrypoint.py"
STUB_BIN = HERE / "stub_bin"
FAKE_SITE = HERE / "fake_site"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "lerobot-train-image.yml"

sys.path.insert(0, str(IMAGE_DIR))
sys.path.insert(0, str(HERE))
import entrypoint  # noqa: E402
import event_contract  # noqa: E402

TOKEN = "hf_TESTtokenABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
WANDB_KEY = "wandb_TESTkey0123456789abcdef0123456789"
DATASET = "team/so101_pick"
DATASET_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
DATASET_V1_SHA = "f0e1d2c3b4a5968778695a4b3c2d1e0f98765432"
MODEL = "team/so101_pick-act"
IMAGE = "ghcr.io/wendylabsinc/lerobot-train:lerobot-0.6.1"
IMAGE_REVISION = "0123abcd0123abcd0123abcd0123abcd0123abcd"
INFO_V3 = {
    "codebase_version": "v3.0",
    "robot_type": "so101_follower",
    "total_episodes": 50,
    "total_frames": 11939,
    "fps": 30,
}

# Captured from LeRobot 0.6.1's own MetricsTracker and init_logging formatter
# (logging a tracker at step 4600 from lerobot/scripts/lerobot_train.py:641
# and :687), and from tqdm 4.70.1 writing LeRobot's bar into a pipe.
REAL_TRACKER_LINE = (
    "INFO 2026-10-22 15:54:41 ot_train.py:641 step:5K smpl:37K ep:142 epch:2.83 loss:0.123 grdn:12.500 "
    "lr:1.0e-05 updt_s:0.200 data_s:0.010 smp/s:38 l1_loss:0.081 kld_loss:1.500"
)
REAL_CHECKPOINT_LINE = "INFO 2026-10-22 15:54:41 ot_train.py:687 Checkpoint policy after step 4600"
REAL_TQDM_SEGMENT = "Training:  26%|███▌      | 13/50 [00:00<00:00, 41.26step/s]"
# lerobot.utils.utils.format_big_number in LeRobot 0.6.1.
REAL_BIG_NUMBERS = [
    (0, "0"),
    (1, "1"),
    (200, "200"),
    (999, "999"),
    (1000, "1K"),
    (1499, "1K"),
    (1500, "2K"),
    (2500, "2K"),
    (4600, "5K"),
    (10000, "10K"),
    (19800, "20K"),
    (20000, "20K"),
    (999999, "1000K"),
    (1000000, "1M"),
]


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str
    events: list[dict]
    line_problems: list[str]

    def names(self) -> list[str]:
        return [e["event"] for e in self.events]

    def of(self, name: str) -> list[dict]:
        return [e for e in self.events if e["event"] == name]


class Harness:
    """A scratch Hub, output directory and environment for one test."""

    def __init__(self, test: unittest.TestCase):
        self.tmp = Path(tempfile.mkdtemp(prefix="wendy-train-test-"))
        test.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.output_dir = self.tmp / "output"
        self.state_file = self.tmp / "hub-state.json"
        self.calls_file = self.tmp / "hub-calls.jsonl"
        self.argv_file = self.tmp / "stub-argv.jsonl"
        (self.tmp / "home").mkdir()
        (self.tmp / "cache").mkdir()
        self.state = {
            "token": TOKEN,
            "writable_namespaces": ["team"],
            "datasets": {
                DATASET: {
                    "refs": {"main": DATASET_SHA, "v1": DATASET_V1_SHA},
                    "commits": [DATASET_SHA, DATASET_V1_SHA],
                    "info": INFO_V3,
                },
                "team/private_ds": {"private": True, "refs": {"main": DATASET_SHA}, "info": INFO_V3},
                "team/gated_ds": {"gated": True, "refs": {"main": DATASET_SHA}, "info": INFO_V3},
                "team/old_format": {"refs": {"main": DATASET_SHA}, "info": dict(INFO_V3, codebase_version="v2.1")},
                "team/not_lerobot": {"refs": {"main": DATASET_SHA}, "info": None},
                "team/other_ds": {"refs": {"main": DATASET_SHA}, "info": INFO_V3},
                "team/flaky": {"server_error": True, "refs": {"main": DATASET_SHA}, "info": INFO_V3},
            },
        }
        self.save_state()

    def save_state(self) -> None:
        self.state_file.write_text(json.dumps(self.state))

    def env(self, **overrides: str | None) -> dict[str, str]:
        env = {
            "PATH": os.pathsep.join([str(STUB_BIN), str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
            "PYTHONPATH": str(FAKE_SITE),
            "HOME": str(self.tmp / "home"),
            "FAKE_HF_STATE": str(self.state_file),
            "FAKE_HF_CALLS": str(self.calls_file),
            "FAKE_HF_CACHE": str(self.tmp / "cache"),
            "STUB_ARGV_FILE": str(self.argv_file),
            "LEROBOT_TRAIN_IMAGE": IMAGE,
            "LEROBOT_TRAIN_IMAGE_REVISION": IMAGE_REVISION,
            "WENDY_TRAIN_OUTPUT_DIR": str(self.output_dir),
            "WENDY_TRAIN_DATASET": DATASET,
            "WENDY_TRAIN_POLICY": "act",
            "WENDY_TRAIN_STEPS": "1000",
            "WENDY_TRAIN_MODEL_REPO": MODEL,
            "HF_TOKEN": TOKEN,
        }
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run(self, *args: str, timeout: float = 60, **overrides: str | None) -> Result:
        proc = subprocess.run(
            [sys.executable, str(ENTRYPOINT), *args],
            env=self.env(**overrides),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        events, problems = event_contract.parse_events(proc.stdout)
        return Result(proc.returncode, proc.stdout, proc.stderr, events, problems)

    def calls(self, name: str | None = None) -> list[dict]:
        if not self.calls_file.exists():
            return []
        calls = [json.loads(line) for line in self.calls_file.read_text().splitlines()]
        return [c for c in calls if name is None or c["call"] == name]

    def argvs(self) -> list[list[str]]:
        if not self.argv_file.exists():
            return []
        return [json.loads(line) for line in self.argv_file.read_text().splitlines()]

    @property
    def checkpoints(self) -> Path:
        return self.output_dir / "train" / "checkpoints"


class EntrypointTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(self)

    def assertSucceeded(self, result: Result, **expect: object) -> None:
        self.assertEqual(
            result.code, 0, f"exit {result.code}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr[-4000:]}"
        )
        self.assertEqual(result.line_problems, [])
        self.assertEqual(event_contract.check_run(result.events, **expect), [])

    def assertFailed(self, result: Result, variable: str | None, code: int | None = None) -> str:
        if code is not None:
            self.assertEqual(result.code, code, result.stdout + result.stderr[-3000:])
        else:
            self.assertNotEqual(result.code, 0, result.stdout)
        self.assertEqual(result.line_problems, [])
        self.assertEqual(event_contract.check_error(result.events, variable), [])
        return result.events[-1]["message"]


class FreshRunTest(EntrypointTestCase):
    def test_trains_and_pushes_with_events_in_order(self) -> None:
        result = self.h.run()
        self.assertSucceeded(result, steps=1000, resumed_from=0, push=True, model=MODEL, max_progress_gap=10)
        # stdout carries nothing but events; lerobot-train's own log goes to stderr.
        self.assertTrue(result.stdout.endswith("\n"))
        for line in result.stdout.splitlines():
            self.assertTrue(line.startswith("WENDY_TRAIN {"), line)
        self.assertIn("Start offline training", result.stderr)

        self.assertEqual(
            result.events[0],
            {
                "event": "start",
                "dataset": DATASET,
                "dataset_revision": DATASET_SHA,
                "policy": "act",
                "steps": 1000,
                "lerobot_version": "0.6.1",
                "resumed_from_step": 0,
            },
        )
        progress = result.of("progress")
        self.assertEqual([e["step"] for e in progress], list(range(10, 1001, 10)))
        self.assertEqual(progress[0]["loss"], 0.909)  # the stub's loss at step 10
        self.assertEqual(progress[-1]["loss"], 0.091)
        self.assertEqual([e["step"] for e in result.of("checkpoint")], [1000])
        self.assertEqual(result.names()[-2:], ["pushing", "done"])
        self.assertEqual(result.of("pushing")[0], {"event": "pushing", "model": MODEL})

        done = result.events[-1]
        commits = self.h.calls("create_commit")
        self.assertEqual(len(commits), 1)
        self.assertEqual(done["revision"], commits[0]["oid"])
        self.assertEqual(done["local_path"], str(self.h.checkpoints / "001000" / "pretrained_model"))
        self.assertTrue(Path(done["local_path"], "model.safetensors").is_file())

    def test_passes_lerobot_train_the_resolved_revision_and_owned_flags(self) -> None:
        result = self.h.run()
        self.assertSucceeded(result)
        (argv,) = self.h.argvs()
        self.assertEqual(
            argv,
            [
                f"--dataset.repo_id={DATASET}",
                "--policy.type=act",
                f"--dataset.revision={DATASET_SHA}",
                f"--output_dir={self.h.output_dir / 'train'}",
                "--steps=1000",
                "--batch_size=8",
                "--log_freq=10",
                "--save_freq=1000",
                "--save_checkpoint=true",
                "--policy.push_to_hub=false",
                f"--policy.repo_id={MODEL}",
            ],
        )

    def test_hub_calls_and_model_card(self) -> None:
        result = self.h.run(WENDY_TRAIN_DATASET_REVISION="v1")
        self.assertSucceeded(result)
        self.assertEqual(result.events[0]["dataset_revision"], DATASET_V1_SHA)
        (info_call,) = self.h.calls("dataset_info")
        self.assertEqual((info_call["repo_id"], info_call["revision"], info_call["token_ok"]), (DATASET, "v1", True))
        (download,) = self.h.calls("hf_hub_download")
        self.assertEqual(
            (download["filename"], download["repo_type"], download["revision"]),
            ("meta/info.json", "dataset", DATASET_V1_SHA),
        )
        (create,) = self.h.calls("create_repo")
        self.assertEqual((create["repo_id"], create["repo_type"], create["exist_ok"]), (MODEL, "model", True))

        (commit,) = self.h.calls("create_commit")
        self.assertEqual(commit["repo_type"], "model")
        self.assertIn(DATASET_V1_SHA[:12], commit["commit_message"])
        self.assertEqual(
            commit["files"],
            sorted(
                [
                    "README.md",
                    "config.json",
                    "model.safetensors",
                    "train_config.json",
                    "policy_preprocessor.json",
                    "policy_preprocessor_step_3_normalizer_processor.safetensors",
                    "policy_postprocessor.json",
                    "policy_postprocessor_step_0_unnormalizer_processor.safetensors",
                ]
            ),
        )
        card = commit["readme"]
        self.assertTrue(card.startswith("---\nlibrary_name: lerobot\n"))
        for expected in (
            f"[`{DATASET}`](https://huggingface.co/datasets/{DATASET}) at commit `{DATASET_V1_SHA}`",
            "| Policy | `act` |",
            "| Training steps | 1000 |",
            "| Batch size | 8 |",
            f"`{IMAGE}` built from wendylabsinc/templates commit `{IMAGE_REVISION}`",
            "| LeRobot version | `0.6.1` |",
            "LeRobotDataset `v3.0` (robot so101_follower, episodes 50, frames 11939, 30 fps)",
            "from lerobot.policies.act.modeling_act import ACTPolicy",
            f'ACTPolicy.from_pretrained("{MODEL}", revision="<commit>")',
        ):
            self.assertIn(expected, card)
        self.assertNotIn("base_model", card)

    def test_smolvla_fine_tunes_the_pinned_base_model(self) -> None:
        result = self.h.run(
            WENDY_TRAIN_POLICY="smolvla", WENDY_TRAIN_STEPS="100", WENDY_TRAIN_MODEL_REPO="team/smolvla"
        )
        self.assertSucceeded(result, steps=100, push=True)
        (argv,) = self.h.argvs()
        self.assertIn("--policy.path=lerobot/smolvla_base", argv)
        self.assertIn(f"--policy.pretrained_revision={entrypoint.SMOLVLA_BASE_REVISION}", argv)
        self.assertIn("--batch_size=64", argv)
        self.assertNotIn("--policy.type=smolvla", argv)
        card = self.h.calls("create_commit")[0]["readme"]
        self.assertIn('base_model: "lerobot/smolvla_base"', card)
        self.assertIn(f"at commit `{entrypoint.SMOLVLA_BASE_REVISION}`", card)
        self.assertIn("from lerobot.policies.smolvla.modeling_smolvla import SmolVLAPolicy", card)

    def test_long_run_reports_every_percent_and_keeps_two_checkpoints(self) -> None:
        result = self.h.run(WENDY_TRAIN_STEPS="20000", STUB_TQDM_EVERY="50", WENDY_TRAIN_BATCH_SIZE="16")
        self.assertSucceeded(result, steps=20000, max_progress_gap=200)
        self.assertEqual([e["step"] for e in result.of("checkpoint")], list(range(2000, 20001, 2000)))
        self.assertEqual(sorted(p.name for p in self.h.checkpoints.iterdir()), ["018000", "020000", "last"])
        self.assertIn("--batch_size=16", self.h.argvs()[0])
        self.assertIn("--save_freq=2000", self.h.argvs()[0])
        # Every checkpoint is preceded by progress at (at least) its step.
        last_progress = 0
        for event in result.events:
            if event["event"] == "progress":
                last_progress = event["step"]
            elif event["event"] == "checkpoint":
                self.assertGreaterEqual(last_progress, event["step"])

    def test_push_false_needs_no_token_or_model(self) -> None:
        result = self.h.run(WENDY_TRAIN_PUSH="false", HF_TOKEN=None, WENDY_TRAIN_MODEL_REPO=None)
        self.assertSucceeded(result, push=False)
        done = result.events[-1]
        self.assertEqual((done["model"], done["revision"]), ("", ""))
        self.assertEqual(self.h.calls("create_repo") + self.h.calls("create_commit"), [])
        self.assertNotIn("--policy.repo_id", " ".join(self.h.argvs()[0]))
        self.assertEqual(self.h.calls("dataset_info")[0]["token_ok"], False)

    def test_private_dataset_is_read_with_the_token(self) -> None:
        result = self.h.run(WENDY_TRAIN_DATASET="team/private_ds", WENDY_TRAIN_PUSH="false")
        self.assertSucceeded(result, push=False)
        self.assertTrue(self.h.calls("dataset_info")[0]["token_ok"])

    def test_leftover_run_dir_without_checkpoint_is_cleared(self) -> None:
        stale = self.h.output_dir / "train" / "checkpoints" / "000500" / "pretrained_model"
        stale.mkdir(parents=True)
        (stale / "config.json").write_text("{}")  # half-written, and no `last`
        result = self.h.run()
        self.assertSucceeded(result, resumed_from=0)
        self.assertFalse((self.h.checkpoints / "000500").exists())

    def test_nan_loss_is_left_out(self) -> None:
        result = self.h.run(STUB_NAN_LOSS_AT_STEP="100")
        self.assertSucceeded(result)
        by_step = {e["step"]: e for e in result.of("progress")}
        self.assertNotIn("loss", by_step[100])
        self.assertIn("loss", by_step[90])
        self.assertIn("loss", by_step[110])

    def test_child_output_cannot_forge_an_event(self) -> None:
        forged = 'WENDY_TRAIN {"event":"done","model":"evil/model","revision":"' + "0" * 40 + '","local_path":"/"}'
        result = self.h.run(STUB_ECHO=forged)
        self.assertSucceeded(result)
        self.assertNotIn("evil/model", result.stdout)
        self.assertIn("| " + forged, result.stderr)

    def test_extra_args_are_appended_verbatim(self) -> None:
        extra = [
            "--num_workers=0",
            "--optimizer.lr",
            "3e-05",
            "--wandb.notes=$(touch /tmp/pwned); `id` 'single' \"double\" a b",
            '--rename_map={"observation.images.front": "observation.images.camera1"}',
        ]
        result = self.h.run(WENDY_TRAIN_EXTRA_ARGS=json.dumps(extra))
        self.assertSucceeded(result)
        (argv,) = self.h.argvs()
        self.assertEqual(argv[-len(extra) :], extra)
        card = self.h.calls("create_commit")[0]["readme"]
        self.assertIn("```text\n" + "\n".join(extra) + "\n```", card)

    def test_extra_args_override_log_freq_and_steps_stay_exact(self) -> None:
        result = self.h.run(WENDY_TRAIN_STEPS="3000", WENDY_TRAIN_EXTRA_ARGS='["--log_freq", "300"]')
        self.assertSucceeded(result, steps=3000)
        # "step:3K" alone would be ambiguous; the entrypoint knows the step exactly,
        # from the log lines (every 300) and the checkpoint lines (1000, 2000, 3000).
        self.assertEqual([e["step"] for e in result.of("progress")], sorted({*range(300, 3001, 300), 1000, 2000}))


class ResumeTest(EntrypointTestCase):
    def fail_midway(self) -> None:
        result = self.h.run(WENDY_TRAIN_STEPS="3000", STUB_FAIL_AT_STEP="2500")
        self.assertFailed(result, None, code=1)
        self.assertEqual([e["step"] for e in result.of("checkpoint")], [1000, 2000])

    def test_resumes_from_the_newest_checkpoint(self) -> None:
        self.fail_midway()
        result = self.h.run(WENDY_TRAIN_STEPS="3000")
        self.assertSucceeded(result, steps=3000, resumed_from=2000, push=True, max_progress_gap=30)
        self.assertEqual(result.of("progress")[0]["step"], 2010)
        self.assertEqual([e["step"] for e in result.of("checkpoint")], [3000])
        argv = self.h.argvs()[-1]
        self.assertEqual(
            argv[:3],
            [
                f"--config_path={self.h.checkpoints / '002000' / 'pretrained_model' / 'train_config.json'}",
                "--resume=true",
                f"--dataset.revision={DATASET_SHA}",
            ],
        )
        self.assertNotIn("--policy.type=act", argv)
        self.assertNotIn(f"--dataset.repo_id={DATASET}", argv)

    def test_resume_keeps_the_dataset_commit_of_the_checkpoint(self) -> None:
        self.fail_midway()
        self.h.state["datasets"][DATASET]["refs"]["main"] = DATASET_V1_SHA  # main moved on
        self.h.save_state()
        result = self.h.run(WENDY_TRAIN_STEPS="3000")
        self.assertSucceeded(result, resumed_from=2000)
        self.assertEqual(result.events[0]["dataset_revision"], DATASET_SHA)
        self.assertIn(f"--dataset.revision={DATASET_SHA}", self.h.argvs()[-1])
        self.assertIn(DATASET_SHA, self.h.calls("create_commit")[0]["readme"])

    def test_skips_half_written_checkpoints(self) -> None:
        self.fail_midway()
        # Stopped while writing 2500: newer than `last`, possibly incomplete.
        partial = self.h.checkpoints / "002500"
        shutil.copytree(self.h.checkpoints / "002000", partial)
        (partial / "training_state" / "training_step.json").write_text('{"step": 2500}')
        # And a directory with files missing.
        broken = self.h.checkpoints / "002600" / "pretrained_model"
        broken.mkdir(parents=True)
        (broken / "config.json").write_text("{}")
        result = self.h.run(WENDY_TRAIN_STEPS="3000")
        self.assertSucceeded(result, resumed_from=2000)

    def test_without_last_link_uses_the_newest_complete_checkpoint(self) -> None:
        self.fail_midway()
        (self.h.checkpoints / "last").unlink()  # stopped while lerobot-train moved it
        result = self.h.run(WENDY_TRAIN_STEPS="3000")
        self.assertSucceeded(result, resumed_from=2000)

    def test_finished_training_is_pushed_without_training_again(self) -> None:
        first = self.h.run(WENDY_TRAIN_PUSH="false")
        self.assertSucceeded(first, push=False)
        result = self.h.run()
        self.assertSucceeded(result, steps=1000, resumed_from=1000, push=True)
        self.assertEqual(result.names(), ["start", "pushing", "done"])
        self.assertEqual(len(self.h.argvs()), 1)  # lerobot-train ran only the first time

    def test_more_steps_continue_a_finished_run(self) -> None:
        self.assertSucceeded(self.h.run(WENDY_TRAIN_PUSH="false"), push=False)
        result = self.h.run(WENDY_TRAIN_STEPS="2000")
        self.assertSucceeded(result, steps=2000, resumed_from=1000)
        self.assertIn("--steps=2000", self.h.argvs()[-1])

    def test_checkpoint_of_another_job_is_refused(self) -> None:
        self.fail_midway()
        result = self.h.run(WENDY_TRAIN_STEPS="3000", WENDY_TRAIN_DATASET="team/other_ds")
        self.assertIn("different job", self.assertFailed(result, "WENDY_TRAIN_OUTPUT_DIR", code=2))
        result = self.h.run(WENDY_TRAIN_STEPS="3000", WENDY_TRAIN_POLICY="smolvla")
        self.assertFailed(result, "WENDY_TRAIN_OUTPUT_DIR", code=2)
        self.assertEqual(len(self.h.argvs()), 1)

    def test_fewer_steps_than_the_checkpoint_is_refused(self) -> None:
        self.fail_midway()
        result = self.h.run(WENDY_TRAIN_STEPS="1500")
        self.assertFailed(result, "WENDY_TRAIN_STEPS", code=2)


class ValidationTest(EntrypointTestCase):
    CASES = [
        ({"WENDY_TRAIN_DATASET": None}, "WENDY_TRAIN_DATASET"),
        ({"WENDY_TRAIN_DATASET": "  "}, "WENDY_TRAIN_DATASET"),
        ({"WENDY_TRAIN_DATASET": "no-namespace"}, "WENDY_TRAIN_DATASET"),
        ({"WENDY_TRAIN_DATASET": "team/../etc"}, "WENDY_TRAIN_DATASET"),
        ({"WENDY_TRAIN_DATASET_REVISION": "main; rm -rf /"}, "WENDY_TRAIN_DATASET_REVISION"),
        ({"WENDY_TRAIN_POLICY": None}, "WENDY_TRAIN_POLICY"),
        ({"WENDY_TRAIN_POLICY": "pi05"}, "WENDY_TRAIN_POLICY"),
        ({"WENDY_TRAIN_STEPS": "abc"}, "WENDY_TRAIN_STEPS"),
        ({"WENDY_TRAIN_STEPS": "0"}, "WENDY_TRAIN_STEPS"),
        ({"WENDY_TRAIN_STEPS": "-5"}, "WENDY_TRAIN_STEPS"),
        ({"WENDY_TRAIN_STEPS": "1e4"}, "WENDY_TRAIN_STEPS"),
        ({"WENDY_TRAIN_BATCH_SIZE": "1.5"}, "WENDY_TRAIN_BATCH_SIZE"),
        ({"WENDY_TRAIN_PUSH": "maybe"}, "WENDY_TRAIN_PUSH"),
        ({"WENDY_TRAIN_MODEL_REPO": None}, "WENDY_TRAIN_MODEL_REPO"),
        ({"WENDY_TRAIN_MODEL_REPO": "bad repo"}, "WENDY_TRAIN_MODEL_REPO"),
        ({"HF_TOKEN": None}, "HF_TOKEN"),
        ({"HF_TOKEN": ""}, "HF_TOKEN"),
        ({"WENDY_TRAIN_EXTRA_ARGS": "--num_workers=0"}, "WENDY_TRAIN_EXTRA_ARGS"),
        ({"WENDY_TRAIN_EXTRA_ARGS": '{"num_workers": 0}'}, "WENDY_TRAIN_EXTRA_ARGS"),
        ({"WENDY_TRAIN_EXTRA_ARGS": '["--num_workers", 0]'}, "WENDY_TRAIN_EXTRA_ARGS"),
        ({"WENDY_TRAIN_EXTRA_ARGS": '["--steps=5"]'}, "WENDY_TRAIN_STEPS"),
        ({"WENDY_TRAIN_EXTRA_ARGS": '["--policy.push_to_hub=true"]'}, "WENDY_TRAIN_EXTRA_ARGS"),
        ({"WENDY_TRAIN_EXTRA_ARGS": '["--output_dir", "/tmp/x"]'}, "WENDY_TRAIN_OUTPUT_DIR"),
        ({"WENDY_TRAIN_EXTRA_ARGS": '["--job.target=hf"]'}, "WENDY_TRAIN_EXTRA_ARGS"),
        ({"WENDY_TRAIN_OUTPUT_DIR": "/dev/null/output"}, "WENDY_TRAIN_OUTPUT_DIR"),
    ]

    def test_bad_input_names_the_variable_before_any_work(self) -> None:
        for overrides, variable in self.CASES:
            with self.subTest(overrides=overrides):
                result = self.h.run(**overrides)
                self.assertFailed(result, variable, code=2)
                self.assertEqual(result.names(), ["error"])
                self.assertEqual(self.h.argvs(), [])
                self.assertEqual(self.h.calls(), [])

    def test_every_problem_is_reported_at_once(self) -> None:
        result = self.h.run(WENDY_TRAIN_DATASET=None, WENDY_TRAIN_POLICY="pi05", WENDY_TRAIN_STEPS="x", HF_TOKEN=None)
        message = self.assertFailed(result, None, code=2)
        for variable in ("WENDY_TRAIN_DATASET", "WENDY_TRAIN_POLICY", "WENDY_TRAIN_STEPS", "HF_TOKEN"):
            self.assertIn(variable, message)

    def test_arguments_are_refused(self) -> None:
        result = self.h.run("--steps=5")
        self.assertIn("takes no arguments", self.assertFailed(result, None, code=2))
        self.assertEqual(self.h.argvs(), [])

    def test_defaults(self) -> None:
        env = {
            "WENDY_TRAIN_DATASET": DATASET,
            "WENDY_TRAIN_POLICY": "act",
            "WENDY_TRAIN_MODEL_REPO": MODEL,
            "HF_TOKEN": TOKEN,
        }
        cfg = entrypoint.parse_config(env)
        self.assertEqual(
            (cfg.dataset_revision, cfg.steps, cfg.batch_size, cfg.push, cfg.output_dir, cfg.extra_args),
            ("main", 20000, 8, True, Path("/workspace/output"), ()),
        )
        cfg = entrypoint.parse_config(
            dict(env, WENDY_TRAIN_POLICY="SmolVLA", WENDY_TRAIN_PUSH="FALSE", WENDY_TRAIN_STEPS="")
        )
        self.assertEqual(
            (cfg.policy, cfg.steps, cfg.batch_size, cfg.push, cfg.model_repo), ("smolvla", 20000, 64, False, "")
        )


class HubErrorTest(EntrypointTestCase):
    def test_hub_problems_name_the_variable_and_stop_before_training(self) -> None:
        cases = [
            ({"WENDY_TRAIN_DATASET": "team/missing"}, "WENDY_TRAIN_DATASET", "was not found"),
            (
                {"WENDY_TRAIN_DATASET": "team/private_ds", "HF_TOKEN": None, "WENDY_TRAIN_PUSH": "false"},
                "WENDY_TRAIN_DATASET",
                "HF_TOKEN is not set",
            ),
            ({"WENDY_TRAIN_DATASET": "team/gated_ds"}, "WENDY_TRAIN_DATASET", "gated"),
            ({"WENDY_TRAIN_DATASET_REVISION": "no-such-branch"}, "WENDY_TRAIN_DATASET_REVISION", "does not exist"),
            ({"WENDY_TRAIN_DATASET": "team/old_format"}, "WENDY_TRAIN_DATASET", "v2.1"),
            ({"WENDY_TRAIN_DATASET": "team/not_lerobot"}, "WENDY_TRAIN_DATASET", "not a LeRobot dataset"),
            ({"HF_TOKEN": "hf_wrongtoken000000000000000000"}, "HF_TOKEN", "rejected"),
            ({"WENDY_TRAIN_MODEL_REPO": "someone-else/model"}, "WENDY_TRAIN_MODEL_REPO", "403"),
        ]
        for overrides, variable, detail in cases:
            with self.subTest(overrides=overrides):
                result = self.h.run(**overrides)
                message = self.assertFailed(result, variable, code=2)
                self.assertIn(detail, message)
                self.assertNotIn("start", result.names())
                self.assertEqual(self.h.argvs(), [])

    def test_push_failure_is_an_error_after_pushing(self) -> None:
        self.h.state.update(commit_failures=5, commit_failure_status=403)
        self.h.save_state()
        result = self.h.run()
        message = self.assertFailed(result, "WENDY_TRAIN_MODEL_REPO", code=1)
        self.assertIn("403", message)
        self.assertEqual(result.names()[-2:], ["pushing", "error"])
        self.assertEqual(len(self.h.calls("create_commit")), 1)  # a 403 is not retried


class SecretTest(EntrypointTestCase):
    def assertNoSecrets(self, *texts: str) -> None:
        for text in texts:
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(WANDB_KEY, text)

    def test_token_never_appears_in_output_or_model_card(self) -> None:
        extra = json.dumps([f"--wandb.notes=token {TOKEN} key {WANDB_KEY}"])
        result = self.h.run(STUB_PRINT_SECRETS="1", WANDB_API_KEY=WANDB_KEY, WENDY_TRAIN_EXTRA_ARGS=extra)
        self.assertSucceeded(result)
        card = self.h.calls("create_commit")[0]["readme"]
        self.assertNoSecrets(result.stdout, result.stderr, card)
        self.assertIn("stub debug: HF_TOKEN=***", result.stderr)
        self.assertIn("token *** key ***", card)
        # lerobot-train itself still gets the real values.
        self.assertIn(f"--wandb.notes=token {TOKEN} key {WANDB_KEY}", self.h.argvs()[0])

    def test_token_is_scrubbed_from_error_messages(self) -> None:
        # A Hub error whose text the entrypoint quotes, and which carries the token.
        self.h.state["leak_token_in_errors"] = True
        self.h.save_state()
        result = self.h.run(WENDY_TRAIN_DATASET="team/flaky")
        message = self.assertFailed(result, "WENDY_TRAIN_DATASET", code=1)
        self.assertIn("500 Server Error", message)
        self.assertIn("Bearer ***", message)
        self.assertNoSecrets(result.stdout, result.stderr)

    def test_token_is_scrubbed_from_a_crash(self) -> None:
        self.h.state["leak_token_in_errors"] = True
        self.h.save_state()
        result = self.h.run(HF_TOKEN="hf_wrongtoken000000000000000000secret")
        self.assertFailed(result, "HF_TOKEN", code=2)
        self.assertNotIn("hf_wrongtoken000000000000000000secret", result.stdout + result.stderr)


class FailureTest(EntrypointTestCase):
    def test_nonzero_exit_status_is_propagated(self) -> None:
        result = self.h.run(STUB_FAIL_AT_STEP="55", STUB_EXIT_CODE="3")
        message = self.assertFailed(result, None, code=3)
        self.assertIn("lerobot-train exited with status 3", message)
        self.assertIn("torch.OutOfMemoryError: CUDA out of memory", message)
        self.assertEqual(result.names()[0], "start")
        self.assertEqual(result.of("progress")[-1]["step"], 50)

    def test_killed_lerobot_train_is_reported(self) -> None:
        result = self.h.run(STUB_KILL_AT_STEP="20")
        message = self.assertFailed(result, None, code=128 + signal.SIGKILL)
        self.assertIn("killed by SIGKILL", message)

    def test_missing_final_checkpoint_is_an_error(self) -> None:
        result = self.h.run(STUB_SKIP_FINAL_CHECKPOINT="1")
        message = self.assertFailed(result, None, code=1)
        self.assertIn("no complete checkpoint for step 1000", message)
        self.assertEqual(self.h.calls("create_commit"), [])

    def test_missing_lerobot_train(self) -> None:
        env_path = os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"])
        result = self.h.run(PATH=env_path)
        self.assertIn("lerobot-train is not on PATH", self.assertFailed(result, None, code=1))

    def test_sigterm_stops_lerobot_train_and_reports_an_error(self) -> None:
        pid_file = self.h.tmp / "stub.pid"
        stderr = open(self.h.tmp / "stderr.log", "w")  # noqa: SIM115 - outlives this block via addCleanup
        self.addCleanup(stderr.close)
        proc = subprocess.Popen(
            [sys.executable, str(ENTRYPOINT)],
            env=self.h.env(STUB_STEP_SLEEP="0.02", STUB_PID_FILE=str(pid_file)),
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        lines = []
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            if '"event":"progress"' in line:
                break
        proc.send_signal(signal.SIGTERM)
        rest, _ = proc.communicate(timeout=30)
        events, problems = event_contract.parse_events("".join(lines) + rest)
        self.assertEqual(problems, [])
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM)
        self.assertEqual(event_contract.check_error(events), [])
        self.assertIn("SIGTERM", events[-1]["message"])
        stub_pid = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(stub_pid, 0)


class ParsingTest(unittest.TestCase):
    """The log parser against lines LeRobot 0.6.1 and tqdm 4.70.1 really print."""

    def monitor(self, total: int, resumed_from: int = 0, log_freq: int | None = 200, clock=time.monotonic):
        out = io.StringIO()
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        monitor = entrypoint.TrainingMonitor(
            entrypoint.Output(entrypoint.Redactor({}), out, io.StringIO()), tmp, total, resumed_from, log_freq, clock
        )
        return monitor, out

    @staticmethod
    def events(out: io.StringIO) -> list[dict]:
        return event_contract.parse_events(out.getvalue())[0]

    def test_format_big_number_matches_lerobot(self) -> None:
        for number, text in REAL_BIG_NUMBERS:
            self.assertEqual(entrypoint.format_big_number(number), text)
            if number:
                self.assertLessEqual(
                    abs(entrypoint.big_number_value(text) - number), 500 * (1000 if number >= 1e6 else 1)
                )

    def test_tracker_line_gives_the_exact_step_and_loss(self) -> None:
        monitor, out = self.monitor(total=20000, resumed_from=4400)
        monitor.on_line(REAL_TRACKER_LINE)
        self.assertEqual(self.events(out), [{"event": "progress", "step": 4600, "total": 20000, "loss": 0.123}])

    def test_rounded_steps_are_disambiguated_by_log_freq(self) -> None:
        self.assertEqual(entrypoint.tracker_step("5K", 4400, 200), 4600)
        self.assertEqual(entrypoint.tracker_step("5K", 4600, 200), 4800)
        self.assertEqual(entrypoint.tracker_step("200", 0, 200), 200)
        self.assertEqual(entrypoint.tracker_step("20K", 19800, 200), 20000)
        # Without a usable log_freq, the rounded value is the best estimate.
        self.assertEqual(entrypoint.tracker_step("5K", 4400, None), 5000)

    def test_checkpoint_line_sets_the_step(self) -> None:
        monitor, out = self.monitor(total=20000, resumed_from=4400)
        monitor.on_line(REAL_CHECKPOINT_LINE)
        self.assertEqual(monitor.step, 4600)

    def test_tqdm_bar_gives_the_exact_step(self) -> None:
        monitor, out = self.monitor(total=50)
        monitor.on_line(REAL_TQDM_SEGMENT)
        self.assertEqual(self.events(out), [{"event": "progress", "step": 13, "total": 50}])

    def test_tqdm_counts_from_the_resumed_step(self) -> None:
        monitor, out = self.monitor(total=100, resumed_from=50)
        monitor.on_line("Training:  26%|██▌       | 13/50 [00:00<00:00, 41.26step/s]")
        self.assertEqual(monitor.step, 63)
        monitor.on_line("Training:  50%|█████     | 5/10 [00:00<00:00, 9.99it/s]")  # some other bar
        self.assertEqual(monitor.step, 63)

    def test_bar_and_log_line_on_one_line(self) -> None:
        # tqdm leaves no newline, so the next log record continues its line.
        monitor, out = self.monitor(total=20000, resumed_from=4400)
        monitor.on_line("Training:  23%|██▎       | 150/15600 [00:30<52:00, 4.95step/s]" + REAL_TRACKER_LINE)
        self.assertEqual(monitor.step, 4600)
        self.assertEqual(self.events(out)[-1]["loss"], 0.123)

    def test_progress_is_rate_limited_to_one_percent_or_thirty_seconds(self) -> None:
        now = [0.0]
        monitor, out = self.monitor(total=10000, log_freq=None, clock=lambda: now[0])
        for n in range(1, 251):  # tqdm lines for steps 1..250
            monitor.on_line(f"Training:   1%|          | {n}/10000 [00:01<00:02, 4.20step/s]")
        self.assertEqual([e["step"] for e in self.events(out)], [100, 200])
        now[0] = 31.0
        monitor.on_line("Training:   3%|          | 251/10000 [00:01<00:02, 4.20step/s]")
        self.assertEqual([e["step"] for e in self.events(out)], [100, 200, 251])

    def test_exception_line_pattern(self) -> None:
        for line in (
            "RuntimeError: CUDA error: out of memory",
            "torch.OutOfMemoryError: CUDA out of memory",
            "KeyboardInterrupt",
            "error: Unknown argument --x",
        ):
            self.assertTrue(entrypoint.EXCEPTION_RE.match(line), line)
        for line in ("INFO 2026-10-22 15:54:41 ot_train.py:641 step:5K", "Traceback (most recent call last):"):
            self.assertFalse(entrypoint.EXCEPTION_RE.match(line), line)

    def test_frequencies(self) -> None:
        self.assertEqual([entrypoint.default_log_freq(s) for s in (3, 100, 1000, 20000, 100000)], [1, 1, 10, 200, 200])
        self.assertEqual([entrypoint.default_save_freq(s) for s in (3, 1500, 20000, 100000)], [1000, 1000, 2000, 10000])
        self.assertEqual(entrypoint.last_int_flag(["--log_freq=5", "--x", "--log_freq", "7"], "log_freq"), 7)
        self.assertIsNone(entrypoint.last_int_flag(["--log_freq=x"], "log_freq"))


class PushRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(entrypoint, "PUSH_RETRY_DELAYS_S", (0.0, 0.0))
        patcher.start()
        self.addCleanup(patcher.stop)
        env = {
            "WENDY_TRAIN_DATASET": DATASET,
            "WENDY_TRAIN_POLICY": "act",
            "WENDY_TRAIN_MODEL_REPO": MODEL,
            "HF_TOKEN": TOKEN,
        }
        self.cfg = entrypoint.parse_config(env)
        self.out = entrypoint.Output(entrypoint.Redactor(env), io.StringIO(), io.StringIO())

    def hub(self, *outcomes: object) -> SimpleNamespace:
        results = list(outcomes)

        def push_model(repo_id, folder, card, message):
            outcome = results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return SimpleNamespace(push_model=push_model, remaining=results)

    @staticmethod
    def http_error(status: int) -> Exception:
        error = OSError(f"{status} error")
        error.response = SimpleNamespace(status_code=status)  # type: ignore[attr-defined]
        return error

    def test_server_errors_are_retried(self) -> None:
        hub = self.hub(self.http_error(503), ConnectionError("reset"), "c" * 40)
        self.assertEqual(entrypoint.push_with_retries(hub, self.cfg, Path("."), "card", "msg", self.out), "c" * 40)

    def test_gives_up_after_three_attempts(self) -> None:
        hub = self.hub(self.http_error(502), self.http_error(502), self.http_error(502))
        with self.assertRaises(entrypoint.TrainError):
            entrypoint.push_with_retries(hub, self.cfg, Path("."), "card", "msg", self.out)

    def test_client_errors_are_not_retried(self) -> None:
        hub = self.hub(self.http_error(403), "c" * 40)
        with self.assertRaises(entrypoint.TrainError):
            entrypoint.push_with_retries(hub, self.cfg, Path("."), "card", "msg", self.out)
        self.assertEqual(hub.remaining, ["c" * 40])

    def test_a_bad_commit_id_is_an_error(self) -> None:
        with self.assertRaises(entrypoint.TrainError):
            entrypoint.push_with_retries(self.hub("not-a-sha"), self.cfg, Path("."), "card", "msg", self.out)


class HubSignatureTest(unittest.TestCase):
    """The entrypoint's Hub calls, checked against the real huggingface_hub's signatures."""

    def setUp(self) -> None:
        try:
            import huggingface_hub
        except ImportError:
            self.skipTest("huggingface_hub is not installed; CI installs the version uv.lock pins")
        if huggingface_hub.__version__.endswith("+fake"):
            self.skipTest("the fake huggingface_hub is on sys.path")
        self.hf = huggingface_hub
        locked = re.search(r'name = "huggingface-hub"\nversion = "([^"]+)"', (IMAGE_DIR / "uv.lock").read_text())
        assert locked is not None
        if huggingface_hub.__version__ != locked.group(1):
            self.skipTest(f"huggingface_hub {huggingface_hub.__version__} is installed; uv.lock pins {locked.group(1)}")

    def test_calls_match_the_real_signatures(self) -> None:
        hf = self.hf
        sha = "b" * 40
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "info.json").write_text(json.dumps(INFO_V3))
        (tmp / "model" / "sub").mkdir(parents=True)
        (tmp / "model" / "model.safetensors").write_bytes(b"w")
        (tmp / "model" / "sub" / "x.json").write_text("{}")
        with (
            mock.patch.object(hf.HfApi, "dataset_info", autospec=True, return_value=SimpleNamespace(sha=sha)) as info,
            mock.patch.object(hf, "hf_hub_download", autospec=True, return_value=str(tmp / "info.json")) as download,
            mock.patch.object(hf.HfApi, "create_repo", autospec=True) as create,
            mock.patch.object(
                hf.HfApi, "create_commit", autospec=True, return_value=SimpleNamespace(oid=sha)
            ) as commit,
        ):
            hub = entrypoint.Hub("hf_token")
            self.assertEqual(hub.resolve_dataset("a/b", "main"), sha)
            self.assertEqual(hub.dataset_info_json("a/b", sha), INFO_V3)
            hub.ensure_model_repo("a/model")
            self.assertEqual(hub.push_model("a/model", tmp / "model", "card", "message"), sha)
        info.assert_called_once()
        download.assert_called_once()
        create.assert_called_once()
        operations = commit.call_args.args[2]
        self.assertEqual([op.path_in_repo for op in operations], ["model.safetensors", "sub/x.json", "README.md"])
        self.assertTrue(all(isinstance(op, hf.CommitOperationAdd) for op in operations))

    def test_error_classes_map_to_variables(self) -> None:
        import httpx
        from huggingface_hub import errors

        response = httpx.Response(404, request=httpx.Request("GET", "https://huggingface.co/api/datasets/a/b"))
        cfg = entrypoint.parse_config(
            {"WENDY_TRAIN_DATASET": "a/b", "WENDY_TRAIN_POLICY": "act", "WENDY_TRAIN_PUSH": "false"}
        )
        cases = [
            (errors.GatedRepoError("gated", response=response), "WENDY_TRAIN_DATASET"),
            (errors.RepositoryNotFoundError("missing", response=response), "WENDY_TRAIN_DATASET"),
            (errors.RevisionNotFoundError("no rev", response=response), "WENDY_TRAIN_DATASET_REVISION"),
        ]
        for exc, variable in cases:
            error = entrypoint.describe_dataset_error(exc, cfg, "main")
            self.assertIsInstance(error, entrypoint.InputError)
            self.assertTrue(str(error).startswith(variable + ":"), str(error))
        self.assertTrue(issubclass(errors.RemoteEntryNotFoundError, errors.EntryNotFoundError))


class PinsTest(unittest.TestCase):
    """The LeRobot version is written in five places; they must agree."""

    def test_versions_agree(self) -> None:
        pyproject = (IMAGE_DIR / "pyproject.toml").read_text()
        pinned = re.search(r'"lerobot\[[a-z,]+\]==([0-9.]+)"', pyproject)
        assert pinned is not None
        version = pinned.group(1)
        lock = (IMAGE_DIR / "uv.lock").read_text()
        self.assertIn(f'name = "lerobot"\nversion = "{version}"', lock)
        dockerfile = (IMAGE_DIR / "Dockerfile").read_text()
        self.assertIn(f"ARG IMAGE_TAG=ghcr.io/wendylabsinc/lerobot-train:lerobot-{version}\n", dockerfile)
        self.assertIn(f'sh.wendy.lerobot.version="{version}"', dockerfile)
        self.assertTrue(entrypoint.DEFAULT_IMAGE.endswith(f":lerobot-{version}"))
        workflow = WORKFLOW.read_text()
        self.assertIn(f"LEROBOT_VERSION: '{version}'", workflow)
        hub = re.search(r'name = "huggingface-hub"\nversion = "([^"]+)"', lock)
        assert hub is not None
        self.assertIn(f"huggingface_hub=={hub.group(1)}", workflow)

    def test_image_contains_the_entrypoint(self) -> None:
        dockerignore = (IMAGE_DIR / ".dockerignore").read_text().split()
        for name in ("pyproject.toml", "uv.lock", "entrypoint.py"):
            self.assertIn(f"!{name}", dockerignore)
        self.assertIn("COPY entrypoint.py ./", (IMAGE_DIR / "Dockerfile").read_text())
        self.assertRegex(entrypoint.SMOLVLA_BASE_REVISION, r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
