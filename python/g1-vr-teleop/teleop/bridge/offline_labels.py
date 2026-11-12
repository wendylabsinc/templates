"""Human-reviewed labels stored beside an immutable teleop recording.

The recorder's JSONL and its manifest are evidence and stay byte-for-byte
unchanged after sealing.  Review labels therefore live in ``<sid>.labels.json``
and are bound to the sealed episode SHA-256.  Readers may merge the sidecar's
mark/outcome rows only after their normal manifest verification succeeds.
"""
from __future__ import annotations

import json
import os


SCHEMA = "wendy.g1.offline-labels.v1"


def session_id_from_episode(path):
    name = os.path.basename(str(path))
    for suffix in (".jsonl.gz", ".jsonl"):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return os.path.splitext(name)[0]


def path_for_episode(path):
    return os.path.join(os.path.dirname(os.path.abspath(str(path))),
                        session_id_from_episode(path) + ".labels.json")


def load(path, *, episode_sha256=None):
    """Return validated sidecar rows, or empty rows when none exists.

    A stale sidecar is rejected instead of silently labelling different bytes.
    The caller supplies ``episode_sha256`` when it has the verified manifest;
    lightweight readers may omit it and still get schema/session validation.
    """
    label_path = path_for_episode(path)
    if not os.path.isfile(label_path):
        return {"path": label_path, "marks": [], "outcomes": [], "review": None}
    with open(label_path, "rt", encoding="utf-8") as fh:
        value = json.load(fh)
    if value.get("schema") != SCHEMA:
        raise ValueError(f"{os.path.basename(label_path)} has unsupported schema")
    sid = session_id_from_episode(path)
    if value.get("session") != sid:
        raise ValueError(f"{os.path.basename(label_path)} names session {value.get('session')!r}, expected {sid!r}")
    bound = value.get("episode_sha256")
    if episode_sha256 is not None and bound != episode_sha256:
        raise ValueError(f"{os.path.basename(label_path)} is bound to different episode bytes")
    marks = value.get("marks") or []
    outcomes = value.get("outcomes") or []
    if not isinstance(marks, list) or not isinstance(outcomes, list):
        raise ValueError(f"{os.path.basename(label_path)} rows must be lists")
    for row in marks:
        if not isinstance(row, dict) or row.get("type") != "mark" \
                or not isinstance(row.get("t"), (int, float)):
            raise ValueError(f"{os.path.basename(label_path)} contains an invalid mark")
    for row in outcomes:
        if not isinstance(row, dict) or row.get("type") != "outcome" \
                or not isinstance(row.get("t"), (int, float)):
            raise ValueError(f"{os.path.basename(label_path)} contains an invalid outcome")
    return {"path": label_path, "marks": marks, "outcomes": outcomes,
            "review": value.get("review"), "value": value}
