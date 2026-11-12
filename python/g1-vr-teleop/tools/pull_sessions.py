#!/usr/bin/env python3
"""Copy sealed teleop sessions off the robot, ready for tools/export_dataset.py.

    python3 tools/pull_sessions.py --robot 10.0.0.12:8443 --out sessions/

For every sealed session not already in --out it downloads, from the teleop
service's HTTPS port:
    <sid>.manifest.json   the seal: a hash of every file below
    <sid>.jsonl           one row per frame: hands, head, targets, commands sent, measured joints
    <sid>.frames.tar      the head-camera JPEGs and their index (absent when none were recorded)

The exporter checks every file against the manifest before reading it, so a
partial or altered copy is refused rather than trained on. Unsealed sessions
(still recording, or stopped without a seal) are listed and skipped.

The robot serves a self-signed certificate, so it is not verified (as
`curl -k` in the README); pass --cacert with the robot's cert.pem to verify it.
"""
import argparse
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request


def fetch(base, step, ctx, key, sid=None, timeout=120):
    q = {k: v for k, v in (("session", sid), ("key", key)) if v}
    url = f"{base}/session/{step}" + ("?" + urllib.parse.urlencode(q) if q else "")
    with urllib.request.urlopen(url, context=ctx, timeout=timeout) as r:
        return r.read()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--robot", required=True, help="host:port of the teleop HTTPS port (HTTPS_PORT)")
    ap.add_argument("--out", required=True, help="local directory for the sessions")
    ap.add_argument("--key", default=os.environ.get("TELEOP_CONTROL_KEY") or None,
                    help="control key, if the robot was deployed with one (default: $TELEOP_CONTROL_KEY)")
    ap.add_argument("--cacert", default=None, help="verify the robot's certificate against this file")
    ap.add_argument("--session", action="append", default=None, help="only this session id (repeatable)")
    args = ap.parse_args(argv)
    ctx = ssl.create_default_context(cafile=args.cacert) if args.cacert else ssl._create_unverified_context()
    base = "https://" + args.robot.removeprefix("https://").rstrip("/")
    os.makedirs(args.out, exist_ok=True)
    try:
        listing = json.loads(fetch(base, "list", ctx, args.key))["sessions"]
    except Exception as e:  # noqa: BLE001 -- say what failed, in one line
        print(f"cannot list sessions at {base}: {e}")
        print("  is the app deployed with RECORDING=on?")
        return 1
    pulled = skipped = 0
    for s in listing:
        sid = s["session"]
        if args.session and sid not in args.session:
            continue
        if not s.get("manifest"):
            print(f"  skip {sid}: not sealed (still recording, or stopped without a seal)")
            skipped += 1
            continue
        manifest_path = os.path.join(args.out, f"{sid}.manifest.json")
        if os.path.exists(manifest_path):
            continue
        manifest_bytes = fetch(base, "manifest", ctx, args.key, sid)
        manifest = json.loads(manifest_bytes)
        episode_name = (manifest.get("episode") or {}).get("file") or f"{sid}.jsonl"
        with open(os.path.join(args.out, os.path.basename(episode_name)), "wb") as fh:
            fh.write(fetch(base, "get", ctx, args.key, sid, timeout=600))
        frames = "no camera frames"
        if manifest.get("camera"):
            blob = fetch(base, "frames", ctx, args.key, sid, timeout=600)
            with open(os.path.join(args.out, f"{sid}.frames.tar"), "wb") as fh:
                fh.write(blob)
            frames = f"frames {len(blob) / 1e6:.1f} MB"
        # The manifest last: its presence is what marks a session as pulled.
        with open(manifest_path, "wb") as fh:
            fh.write(manifest_bytes)   # byte for byte, as sealed
        print(f"  {sid}: {s.get('bytes', 0) / 1e6:.1f} MB rows, {frames}")
        pulled += 1
    print(f"pulled {pulled} session(s) into {args.out}" + (f", skipped {skipped} unsealed" if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
