"""Publish one deterministic, content-addressed archive per catalog variant."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import pathlib
import subprocess
import tarfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def build(output: pathlib.Path, root: pathlib.Path = ROOT) -> dict:
    catalog = json.loads((root / "meta.json").read_text())
    names = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root
    ).decode().split("\0")
    files = sorted({name for name in names if name and (root / name).is_file()})
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    index = {"version": 1, "revision": revision, "catalog": catalog, "bundles": {}}
    (output / "bundles").mkdir(parents=True, exist_ok=True)
    for template in catalog["templates"]:
        for language in template["languages"]:
            key = f"{language}/{template['name']}"
            selected = [name for name in files if name.startswith(key + "/")]
            if f"{key}/template.json" not in selected:
                raise ValueError(f"Missing manifest for {key}")
            buffer = io.BytesIO()
            with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0, filename="") as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as archive:
                    for name in selected:
                        source = root / name
                        if source.is_symlink():
                            raise ValueError(f"Template symlinks are unsupported: {name}")
                        content = source.read_bytes()
                        entry = tarfile.TarInfo("templates/" + name)
                        entry.size = len(content)
                        entry.mode = 0o755 if source.stat().st_mode & 0o111 else 0o644
                        archive.addfile(entry, io.BytesIO(content))
            data = buffer.getvalue()
            digest = hashlib.sha256(data).hexdigest()
            path = f"bundles/{digest}.tar.gz"
            (output / path).write_bytes(data)
            index["bundles"][key] = {"path": path, "sha256": digest, "size": len(data)}
    (output / "template-index.json").write_text(json.dumps(index, indent=2) + "\n")
    return index


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    result = build(args.output)
    print(f"Built {len(result['bundles'])} bundles for {result['revision']}")
