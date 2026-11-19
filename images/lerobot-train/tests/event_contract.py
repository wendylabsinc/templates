#!/usr/bin/env python3
"""Checks the `WENDY_TRAIN {json}` lines of one run against the contract.

The unit tests use it, and so does the CI smoke train, on the image's real
stdout:

    python3 event_contract.py run.out --steps 6 --resumed-from 3 --push false
    python3 event_contract.py run.out --expect-error WENDY_TRAIN_POLICY

It exits 1 and lists every problem when the stream breaks the contract.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys

PREFIX = "WENDY_TRAIN "
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

# event -> (required fields with their types, optional fields with their types)
SCHEMA: dict[str, tuple[dict[str, type | tuple[type, ...]], dict[str, type | tuple[type, ...]]]] = {
    "start": (
        {
            "dataset": str,
            "dataset_revision": str,
            "policy": str,
            "steps": int,
            "lerobot_version": str,
            "resumed_from_step": int,
        },
        {},
    ),
    "progress": ({"step": int, "total": int}, {"loss": (int, float)}),
    "checkpoint": ({"step": int}, {}),
    "pushing": ({"model": str}, {}),
    "done": ({"model": str, "revision": str, "local_path": str}, {}),
    "error": ({"message": str}, {}),
}


def parse_events(text: str) -> tuple[list[dict], list[str]]:
    """The events in `text`, and problems with the lines themselves."""
    events, problems = [], []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.startswith(PREFIX):
            continue
        body = line[len(PREFIX) :]
        try:
            event = json.loads(body)
        except json.JSONDecodeError as exc:
            problems.append(f"line {number}: not JSON ({exc.msg}): {line}")
            continue
        if not isinstance(event, dict):
            problems.append(f"line {number}: not a JSON object: {line}")
            continue
        if json.dumps(event, separators=(",", ":")) != body:
            problems.append(f"line {number}: not compact JSON: {line}")
        events.append(event)
    return events, problems


def _type_ok(value: object, expected: type | tuple[type, ...]) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, expected):
        return False
    return not isinstance(value, float) or math.isfinite(value)


def check_shapes(events: list[dict]) -> list[str]:
    problems = []
    for index, event in enumerate(events):
        name = event.get("event")
        if name not in SCHEMA:
            problems.append(f"event {index}: unknown event {name!r}")
            continue
        required, optional = SCHEMA[name]
        for field, expected in required.items():
            if field not in event:
                problems.append(f"event {index} ({name}): missing {field}")
            elif not _type_ok(event[field], expected):
                problems.append(f"event {index} ({name}): {field}={event[field]!r} has the wrong type")
        for field, expected in optional.items():
            if field in event and not _type_ok(event[field], expected):
                problems.append(f"event {index} ({name}): {field}={event[field]!r} has the wrong type")
        extra = set(event) - {"event"} - set(required) - set(optional)
        if extra:
            problems.append(f"event {index} ({name}): unexpected fields {sorted(extra)}")
    return problems


def check_run(
    events: list[dict],
    *,
    steps: int | None = None,
    resumed_from: int | None = None,
    push: bool | None = None,
    model: str | None = None,
    max_progress_gap: int | None = None,
) -> list[str]:
    """Checks a run that should have succeeded."""
    problems = check_shapes(events)
    if problems:
        return problems
    names = [e["event"] for e in events]
    if not names or names[0] != "start":
        return problems + [f"the first event is not start: {names[:3]}"]
    if names[-1] != "done":
        return problems + [f"the last event is not done: {names[-3:]}"]
    if "error" in names:
        problems.append("a successful run reported an error")
    if names.count("start") != 1 or names.count("done") != 1:
        problems.append(f"start and done must appear once: {names}")

    start, done = events[0], events[-1]
    total = start["steps"]
    if steps is not None and total != steps:
        problems.append(f"start.steps is {total}, expected {steps}")
    if resumed_from is not None and start["resumed_from_step"] != resumed_from:
        problems.append(f"start.resumed_from_step is {start['resumed_from_step']}, expected {resumed_from}")
    if not COMMIT_RE.match(start["dataset_revision"]):
        problems.append(f"start.dataset_revision {start['dataset_revision']!r} is not a 40-hex commit")
    if not start["lerobot_version"]:
        problems.append("start.lerobot_version is empty")

    middle = names[1:-1]
    pushed = bool(middle) and middle[-1] == "pushing"
    if pushed:
        middle = middle[:-1]
    if any(n not in ("progress", "checkpoint") for n in middle):
        problems.append(f"unexpected order: {names}")
    if push is not None and pushed != push:
        problems.append(f"pushing {'missing' if push else 'reported'} with push={push}")

    previous = start["resumed_from_step"]
    trained = previous < total
    progress = [e for e in events if e["event"] == "progress"]
    for event in progress:
        if event["total"] != total:
            problems.append(f"progress total {event['total']} != start.steps {total}")
        if not previous < event["step"] <= total:
            problems.append(f"progress step {event['step']} does not advance from {previous} within {total}")
        if max_progress_gap is not None and event["step"] - previous > max_progress_gap:
            problems.append(f"progress jumped from {previous} to {event['step']} (more than {max_progress_gap})")
        previous = event["step"]
    checkpoints = [e["step"] for e in events if e["event"] == "checkpoint"]
    if checkpoints != sorted(set(checkpoints)) or any(not 0 < s <= total for s in checkpoints):
        problems.append(f"checkpoint steps are not increasing within 1..{total}: {checkpoints}")
    if trained:
        if not progress or progress[-1]["step"] != total:
            problems.append(f"the last progress event is not step {total}")
        if not checkpoints or checkpoints[-1] != total:
            problems.append(f"no checkpoint event for the final step {total}")

    if pushed:
        if not done["model"] or (model is not None and done["model"] != model):
            problems.append(f"done.model is {done['model']!r}, expected {model!r}")
        if not COMMIT_RE.match(done["revision"]):
            problems.append(f"done.revision {done['revision']!r} is not a 40-hex commit")
        if events[-2]["model"] != done["model"]:
            problems.append("pushing.model and done.model differ")
    elif done["model"] or done["revision"]:
        problems.append("done.model and done.revision must be empty without a push")
    if not done["local_path"]:
        problems.append("done.local_path is empty")
    return problems


def check_error(events: list[dict], variable: str | None = None) -> list[str]:
    """Checks a run that should have failed, naming `variable`."""
    problems = check_shapes(events)
    names = [e.get("event") for e in events]
    if not names or names[-1] != "error":
        return problems + [f"the last event is not error: {names}"]
    if "done" in names:
        problems.append("a failed run reported done")
    if variable is not None and variable not in events[-1]["message"]:
        problems.append(f"the error does not name {variable}: {events[-1]['message']!r}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stdout_file")
    parser.add_argument("--steps", type=int)
    parser.add_argument("--resumed-from", type=int)
    parser.add_argument("--push", choices=("true", "false"))
    parser.add_argument("--model")
    parser.add_argument("--expect-error", metavar="VARIABLE", nargs="?", const="")
    args = parser.parse_args()

    with open(args.stdout_file, encoding="utf-8") as f:
        events, problems = parse_events(f.read())
    if args.expect_error is not None:
        problems += check_error(events, args.expect_error or None)
    else:
        problems += check_run(
            events,
            steps=args.steps,
            resumed_from=args.resumed_from,
            push=None if args.push is None else args.push == "true",
            model=args.model,
        )
    for event in events:
        print(json.dumps(event))
    if problems:
        for problem in problems:
            print(f"contract violation: {problem}", file=sys.stderr)
        return 1
    print(f"{len(events)} events follow the contract")
    return 0


if __name__ == "__main__":
    sys.exit(main())
