"""Seal one recorded session into a hash-locked manifest, and verify one later.

Why a manifest at all. The consumer of these recordings is the coke demo
pipeline, and its integration contract treats schemas and SHA-256 digests as
gates: a stage may consume the previous stage's artifact only while every
digest still matches. Until now an episode here was a file and a paragraph in
episodes/README.md. Nothing said whether the file had been edited since the
run, whether the camera frames beside it were the ones recorded with it, or
which configuration produced it -- and the README already carries one
paragraph explaining that three files' wrist columns cannot be trusted, found
by reading them. A seal makes that a `verify` exit code instead.

What is sealed: the episode file's digest, every camera frame's digest, the
index digest, the header row verbatim (the run's configuration, which no frame
can reproduce), the footer, the counts a trainer selects on (ARMED frames,
attempts, marks, outcomes) and the validator's verdict at seal time.

Two rules the obvious implementation gets wrong:

A manifest is immutable. Sealing an unchanged session again returns the
existing manifest untouched, original `sealed_at` included -- a reseal is not
a new seal. Sealing a CHANGED session over its manifest is refused; the
manifest names what was recorded, and if the file differs the file is what
changed. Move the old manifest aside deliberately if that is really wanted.

Verify recomputes, it does not trust. Every digest is recomputed from the
bytes on disk and the header is re-read from the episode; a manifest edited
by hand to match a tampered file fails on the digest, a file edited to match
a manifest fails on the header. Stdlib plus numpy only, so it runs in the
bridge image at session stop.
"""
import gzip
import hashlib
import json
import os
import subprocess
import time

import episode_check

SCHEMA = "wendy.g1.real-demonstration.v1"
REPO = "g1-vr-teleop"
_CHUNK = 1 << 20   # a 55 MB session is hashed in 1 MiB pieces, never read whole


class SealRefused(ValueError):
    """The session differs from the manifest already sealed beside it."""


# ---- paths --------------------------------------------------------------------

def session_id(session_path):
    name = os.path.basename(str(session_path))
    for suffix in (".jsonl.gz", ".jsonl"):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def manifest_path(session_path):
    return os.path.join(os.path.dirname(os.path.abspath(str(session_path))),
                        session_id(session_path) + ".manifest.json")


def frames_dir(session_path):
    """The camera frames directory the recorder writes beside a session, or None."""
    d = os.path.join(os.path.dirname(os.path.abspath(str(session_path))),
                     session_id(session_path) + ".frames")
    return d if os.path.isdir(d) else None


# ---- digests ------------------------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _content_digest(path, decompress=False):
    """-> (sha256, size) of the file's bytes, or of its decompressed content."""
    h = hashlib.sha256()
    size = 0
    op = gzip.open if decompress else open
    with op(path, "rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


def _camera_digests(d, files="all"):
    """-> (n_frames, frames_sha256, index_sha256).

    One digest over the sorted "name:sha256" lines of the frame files, rather
    than a digest per file in the manifest: a session is fifteen frames a
    second and a manifest listing five thousand hashes is unreadable, while a
    single digest still moves if any frame is altered, added or removed.

    `files`: "all" (every file in the dir except index.jsonl -- JPEGs, the
    depth PNGs beside them, calibration*.json) is what new seals write, as
    `camera.digest_files`; "jpg" is the pre-depth rule, applied when a
    manifest does not say, so a manifest sealed over JPEGs alone still
    verifies after a depth file appears beside them. n_frames is always the
    JPEG count: that is what the footer's cam_frames counts.
    """
    all_names = sorted(n for n in os.listdir(d) if n != "index.jsonl"
                       and os.path.isfile(os.path.join(d, n)))
    jpgs = [n for n in all_names if n.endswith(".jpg")]
    names = all_names if files == "all" else jpgs
    lines = [f"{n}:{sha256_file(os.path.join(d, n))}" for n in names]
    frames_sha = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    index = os.path.join(d, "index.jsonl")
    index_sha = sha256_file(index) if os.path.exists(index) else None
    return len(jpgs), frames_sha, index_sha


def _hand_camera_digests(frames_d):
    """-> {name: {dir, frames, frames_sha256, index_sha256}} for each hand
    camera directory (<sid>.frames/hand-<name>/), or None when there are none.

    Hand cameras are sealed the same way as the head camera, one directory
    each. The head camera's digest ignores subdirectories, so a manifest
    sealed before hand cameras existed still verifies."""
    if frames_d is None:
        return None
    out = {}
    for sub in sorted(os.listdir(frames_d)):
        path = os.path.join(frames_d, sub)
        if not (sub.startswith("hand-") and os.path.isdir(path)):
            continue
        n, frames_sha, index_sha = _camera_digests(path, files="all")
        out[sub[len("hand-"):]] = {"dir": os.path.basename(frames_d) + "/" + sub,
                                   "frames": n, "frames_sha256": frames_sha,
                                   "index_sha256": index_sha}
    return out or None


def _source():
    """Which code recorded this. In the image there is no .git, so the deploy
    passes the commit through G1_TELEOP_COMMIT and whether the tree it built
    from had uncommitted changes through G1_TELEOP_DIRTY ("1"/"0"). None means
    unknown, never a guess: a manifest that says `dirty: false` because nobody
    set the variable would be a clean bill of health nobody issued.

    The eight committed manifests were sealed beside a .git and say
    `commit: 4003bc6, dirty: true`; the first on-device seals said
    `commit: null`, which is what this exists to end. The dirty flag alone is
    ignored without a commit -- it says nothing about which code ran."""
    commit = os.environ.get("G1_TELEOP_COMMIT") or None
    dirty = None
    if commit is not None:
        flag = os.environ.get("G1_TELEOP_DIRTY", "")
        dirty = {"1": True, "0": False}.get(flag.strip(), None)
    if commit is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            commit = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                                    capture_output=True, text=True, timeout=5,
                                    check=True).stdout.strip() or None
            status = subprocess.run(["git", "-C", root, "status", "--porcelain"],
                                    capture_output=True, text=True, timeout=5,
                                    check=True).stdout
            dirty = bool(status.strip())
        except (OSError, subprocess.SubprocessError):
            commit, dirty = None, None
    return {"repo": REPO, "commit": commit, "dirty": dirty}


# ---- reading the episode --------------------------------------------------------

def _read_rows(path):
    op = gzip.open if str(path).endswith(".gz") else open
    with op(path, "rt") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def _read_header(path):
    """Only the first line: verify re-reads the header of a 55 MB file without
    parsing the rest."""
    op = gzip.open if str(path).endswith(".gz") else open
    with op(path, "rt") as fh:
        for line in fh:
            if line.strip():
                row = json.loads(line)
                return row if row.get("type") == "header" else None
    return None


def _counts(rows):
    frames = [r for r in rows if r.get("type") not in ("header", "footer", "mark", "outcome")]
    marks = [r for r in rows if r.get("type") == "mark"]
    outcomes = {}
    for r in rows:
        if r.get("type") == "outcome":
            k = str(r.get("result") or r.get("outcome") or "unknown")
            outcomes[k] = outcomes.get(k, 0) + 1
    armed = sum(1 for r in frames if (r.get("robot") or {}).get("state") == "ARMED")
    joints = set()
    for r in frames:
        joints.update(k for k in (r.get("targets") or {}))
    counts = {"frames": len(frames), "armed_frames": armed, "marks": len(marks),
              "outcomes": outcomes,
              "attempts": episode_check.attempts(frames, marks)}
    extra = {
        "duration_s": round(float(frames[-1]["t"]) - float(frames[0]["t"]), 3) if len(frames) > 1 else 0.0,
        "joints": sorted(joints),
        "has_measured_q": any((r.get("robot") or {}).get("q") for r in frames),
        "has_wrist_ref": any("wrist_ref" in r for r in frames),
        "has_can": any("can" in r for r in frames),
    }
    return counts, extra


# ---- seal / verify --------------------------------------------------------------

_IDENTITY = ("schema", "session", "header")   # plus episode.sha256 and camera digests


def _identity(m):
    cam = m.get("camera") or {}
    hand = tuple(sorted((name, c.get("frames_sha256"), c.get("index_sha256"))
                        for name, c in (m.get("hand_cameras") or {}).items()))
    return ({k: m.get(k) for k in _IDENTITY},
            (m.get("episode") or {}).get("sha256"),
            cam.get("frames_sha256"), cam.get("index_sha256"), hand)


def build(session_path, now=None):
    """The manifest for a session as it is on disk right now. Pure: writes nothing."""
    session_path = os.path.abspath(str(session_path))
    if not os.path.isfile(session_path):
        raise FileNotFoundError(session_path)
    rows = _read_rows(session_path)
    header = rows[0] if rows and rows[0].get("type") == "header" else None
    footer = rows[-1] if rows and rows[-1].get("type") == "footer" else None
    counts, extra = _counts(rows)
    camera = None
    d = frames_dir(session_path)
    if d is not None:
        n, frames_sha, index_sha = _camera_digests(d, files="all")
        depth_n = sum(1 for f in os.listdir(d) if f.endswith(".depth.png"))
        cal = os.path.join(d, "calibration.json")
        camera = {"dir": os.path.basename(d), "frames": n,
                  "frames_sha256": frames_sha, "index_sha256": index_sha,
                  # Which files the frames digest covers. Absent in manifests
                  # sealed before depth existed; verify() reads "jpg" then.
                  "digest_files": "all",
                  "depth_frames": depth_n,
                  "calibration_sha256": sha256_file(cal) if os.path.isfile(cal) else None}
    m = {"schema": SCHEMA,
         "session": session_id(session_path),
         "sealed_at": float(time.time() if now is None else now),
         "source": _source(),
         "episode": {"file": os.path.basename(session_path),
                     "bytes": os.path.getsize(session_path),
                     "sha256": sha256_file(session_path),
                     "gzip": session_path.endswith(".gz")},
         "header": header,
         "footer": footer,
         "counts": counts,
         "camera": camera,
         # Absent (None) in sessions without hand cameras and in every
         # manifest sealed before they existed.
         "hand_cameras": _hand_camera_digests(d),
         "check": episode_check.check(session_path)}
    m.update(extra)
    return m


def seal(session_path, out_path=None, now=None):
    """Write <sid>.manifest.json beside the session and return the manifest.

    Unchanged session, existing manifest -> that manifest, untouched.
    Changed session, existing manifest -> SealRefused.
    """
    out_path = out_path or manifest_path(session_path)
    m = build(session_path, now=now)
    if os.path.exists(out_path):
        with open(out_path, "rt") as fh:
            existing = json.load(fh)
        if _identity(existing) == _identity(m):
            return existing
        raise SealRefused(
            f"{os.path.basename(out_path)} already seals a different {m['session']}; "
            "the session changed after it was sealed. Refusing to overwrite.")
    # Atomic: a manifest is either there and complete or not there.
    tmp = out_path + ".tmp"
    with open(tmp, "wt") as fh:
        json.dump(m, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, out_path)
    return m


def verify(manifest_path_):
    """Recompute everything the manifest claims. -> {ok, reasons, ...}."""
    manifest_path_ = os.path.abspath(str(manifest_path_))
    rep = {"path": os.path.basename(manifest_path_), "ok": False, "reasons": [],
           "session": None, "episode_sha256": None}
    reasons = rep["reasons"]
    try:
        with open(manifest_path_, "rt") as fh:
            m = json.load(fh)
    except (OSError, ValueError) as e:
        reasons.append(f"manifest unreadable: {e}")
        return rep
    rep["session"] = m.get("session")
    if m.get("schema") != SCHEMA:
        reasons.append(f"schema is {m.get('schema')!r}, expected {SCHEMA!r}")
        return rep
    base = os.path.dirname(manifest_path_)
    ep = m.get("episode") or {}
    ep_path = os.path.join(base, str(ep.get("file") or ""))
    # A gzipped COPY of a plain-sealed episode verifies by its content. The
    # bridge seals the plain <sid>.jsonl it wrote; the laptop stores episodes
    # gzipped (a session is 20-55 MB raw). Without this the only way to keep
    # the repo's convention was to re-seal on the laptop -- which is how the
    # first camera episode ended up with a manifest saying `camera: null` and
    # a laptop commit as its source while the bridge's seal, with the frame
    # digests, was thrown away. The seal is on the bytes the recorder wrote;
    # gzip is transport. (Only this direction: a gz-sealed episode is verified
    # as the gz bytes, exactly as before.)
    decompress = False
    if ep.get("file") and not os.path.isfile(ep_path) and not ep.get("gzip") \
            and os.path.isfile(ep_path + ".gz"):
        ep_path, decompress = ep_path + ".gz", True
        rep["stored_as"] = "gzip copy"
    if not ep.get("file") or not os.path.isfile(ep_path):
        reasons.append(f"episode missing: {ep.get('file')}")
    else:
        actual, size = _content_digest(ep_path, decompress)
        if size != ep.get("bytes"):
            reasons.append(f"episode bytes {size} != sealed {ep.get('bytes')}")
        rep["episode_sha256"] = actual
        if actual != ep.get("sha256"):
            reasons.append(f"episode sha256 {actual[:12]}... != sealed {str(ep.get('sha256'))[:12]}...")
        else:
            # Only worth reading the header when the bytes match; if they do
            # not, the header check would just say so a second time.
            try:
                header = _read_header(ep_path)
            except (OSError, ValueError) as e:
                header, reasons = None, reasons + [f"episode header unreadable: {e}"]
            if header != m.get("header"):
                reasons.append("header row differs from the sealed header")
    cam = m.get("camera")
    if cam is not None:
        d = os.path.join(base, str(cam.get("dir") or ""))
        if not cam.get("dir") or not os.path.isdir(d):
            reasons.append(f"camera frames dir missing: {cam.get('dir')}")
        else:
            n, frames_sha, index_sha = _camera_digests(d, files=cam.get("digest_files") or "jpg")
            if n != cam.get("frames"):
                reasons.append(f"camera frames {n} != sealed {cam.get('frames')}")
            if frames_sha != cam.get("frames_sha256"):
                reasons.append("camera frames sha256 differs: a frame was altered, added or removed")
            if index_sha != cam.get("index_sha256"):
                reasons.append("camera index.jsonl sha256 differs")
            if cam.get("calibration_sha256"):
                calp = os.path.join(d, "calibration.json")
                if not os.path.isfile(calp):
                    reasons.append("camera calibration.json missing")
                elif sha256_file(calp) != cam["calibration_sha256"]:
                    reasons.append("camera calibration.json sha256 differs")
    for name, hc in sorted((m.get("hand_cameras") or {}).items()):
        d = os.path.join(base, str(hc.get("dir") or ""))
        if not hc.get("dir") or not os.path.isdir(d):
            reasons.append(f"{name} hand camera frames dir missing: {hc.get('dir')}")
            continue
        n, frames_sha, index_sha = _camera_digests(d, files="all")
        if n != hc.get("frames"):
            reasons.append(f"{name} hand camera frames {n} != sealed {hc.get('frames')}")
        if frames_sha != hc.get("frames_sha256"):
            reasons.append(f"{name} hand camera frames sha256 differs: "
                           "a frame was altered, added or removed")
        if index_sha != hc.get("index_sha256"):
            reasons.append(f"{name} hand camera index.jsonl sha256 differs")
    rep["ok"] = not reasons
    return rep


def summary(rep):
    """One line, in the style of episode_check.summary."""
    if "reasons" in rep:
        head = f"{rep['path']}: {'OK' if rep['ok'] else 'FAIL'}  session {rep.get('session')}"
        return head + "".join(f"\n  {r}" for r in rep["reasons"])
    c = rep.get("counts") or {}
    cam = rep.get("camera") or {}
    hand = rep.get("hand_cameras") or {}
    hand_s = "".join(f", {n} hand frames {h.get('frames', 0)}" for n, h in sorted(hand.items()))
    return (f"{rep['session']}: sealed  {c.get('frames')} frames, ARMED {c.get('armed_frames')}, "
            f"{len(c.get('attempts') or ())} attempts ({c.get('marks')} marked), "
            f"outcomes {c.get('outcomes') or {}}, camera frames {cam.get('frames', 0)}{hand_s}, "
            f"sha256 {rep['episode']['sha256'][:12]}..., check {'OK' if (rep.get('check') or {}).get('ok') else 'ISSUES'}")
