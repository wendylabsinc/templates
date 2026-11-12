"""Validate one recorded session: is it a usable demonstration, and if not, why.

Written after validating the first can sessions by hand found two defects
nothing had flagged: an untracked hand's finger targets copied into 3226 rows,
and 37 % of armed frames refused by the onboard with no reason recorded. A
recording that is wrong in a way no one notices until training is the most
expensive kind of wrong, so this runs on the bridge at every session stop and
the verdict comes back in the stop reply.

Numpy only, so it runs in the bridge image. Reads .jsonl and .jsonl.gz.
"""
import gzip
import json
import math
import os

import numpy as np

ARM_JOINTS = [f"{s}_{j}_joint" for s in ("left", "right")
              for j in ("shoulder_pitch", "shoulder_roll", "shoulder_yaw", "elbow",
                        "wrist_roll", "wrist_pitch", "wrist_yaw")]
FINGERS = {s: [f"{s}_hand_{j}_joint" for j in ("thumb_0", "thumb_1", "thumb_2",
                                                "middle_0", "middle_1", "index_0", "index_1")]
           for s in ("left", "right")}

# Thresholds. Each one was chosen against a real session, not a guess.
GAP_S = 1.0             # a hole this long inside a session is a pause, not a frame drop
LOW_USABLE = 0.5        # under half the rows ARMED: mostly not a demonstration
LOW_APPLIED = 0.8       # the onboard refused more than a fifth of what it read
STALE_STATE_S = 0.1     # measured joints older than this at p90: state not aligned
LIMIT_TOL = 1e-3        # radians past a limit before it counts (rounding sits at 0)
DEPTH_MISSING_FRAC = 0.2  # more camera frames than this without depth: the fetch is losing the race
# Fingers. The knuckles fell from 98 % follow to 12-17 % inside one session
# (DEBUG-FINGERS.md); this is that analysis, run on every stop.
DERATE_MIN_ROWS = 2000  # fewer armed rows with measured fingers cannot show a trend
DERATE_RATIO = 0.5      # last third under half the first third: the joint gave up
HOT_C = 60              # Dex3 motor temperature that deserves a look


def _limits():
    try:
        import retarget_g1 as rg
        lim = dict(rg.ARM_JOINT_LIMITS)
        lim.update(rg.DEX3_RANGE)
        return lim
    except Exception:  # noqa: BLE001 -- the check must run without the retargeter's deps
        return {}


def load(path):
    op = gzip.open if str(path).endswith(".gz") else open
    with op(path, "rt") as fh:
        rows = [json.loads(l) for l in fh if l.strip()]
    header = rows[0] if rows and rows[0].get("type") == "header" else None
    footer = rows[-1] if rows and rows[-1].get("type") == "footer" else None
    # A frame is a row with no type. Anything typed -- header, footer, mark,
    # outcome, whatever comes next -- is not a frame, and counting it as one
    # tripped footer_mismatch the first time a new row type appeared.
    body = [r for r in rows if not r.get("type")]
    marks = [r for r in rows if r.get("type") == "mark"]
    outcomes = [r for r in rows if r.get("type") == "outcome"]
    marks.sort(key=lambda row: float(row.get("t", 0.0)))
    outcomes.sort(key=lambda row: float(row.get("t", 0.0)))
    return header, body, footer, marks, outcomes


def attempts(frames, marks, gap_s=GAP_S, outcomes=()):
    """Split at marks and at pauses, then attach each outcome to its attempt;
    the rule offline replay tools use too."""
    if not frames:
        return []
    cuts = [(float(m["t"]), m.get("label") or "mark") for m in marks
            if isinstance(m.get("t"), (int, float))]
    for a, b in zip(frames, frames[1:]):
        if b["t"] - a["t"] > gap_s:
            cuts.append((float(b["t"]) - 1e-9, "after pause"))
    # A HOLD is a pause the rows do not show: the operator homed or froze the
    # arm mid-attempt, the recorder kept writing HOLDING rows, and the ARMED
    # rows resume seconds later. Left as one attempt, its grid ran across the
    # interlude with no ARMED row to sample and the exporters refused it for
    # "holes" (20260916-001041: a 3.8 s home inside a 45 s attempt). A run of
    # non-ARMED rows longer than gap_s between two ARMED rows is therefore a
    # cut at the first ARMED row after it, and the outcome that follows lands
    # on the span that earned it, not on the aborted approach before the hold.
    last_armed_t, saw_other = None, False
    for r in frames:
        st = (r.get("robot") or {}).get("state")
        if st is None:
            continue                      # older files: no state, no state cuts
        if st == "ARMED":
            if last_armed_t is not None and saw_other and r["t"] - last_armed_t > gap_s:
                cuts.append((float(r["t"]) - 1e-9, "after hold"))
            last_armed_t, saw_other = r["t"], False
        else:
            saw_other = True
    cuts.sort()
    out, start, label = [], 0, "start"

    def seg(i0, i1, lab):
        s = frames[i0:i1]
        armed = sum(1 for r in s if (r.get("robot") or {}).get("state") == "ARMED")
        # t0/t1 so a manifest can point at an attempt in the file without the
        # reader re-deriving the cuts.
        return {"label": lab, "frames": len(s), "seconds": round(s[-1]["t"] - s[0]["t"], 2),
                "t0": float(s[0]["t"]), "t1": float(s[-1]["t"]),
                "armed_frac": round(armed / len(s), 3)}
    for t_cut, lab in cuts:
        idx = next((i for i in range(start, len(frames)) if frames[i]["t"] >= t_cut), len(frames))
        if idx > start:
            out.append(seg(start, idx, label))
        start, label = idx, lab
    if start < len(frames):
        out.append(seg(start, len(frames), label))
    return attach_outcomes(out, outcomes)


def attach_outcomes(atts, outcomes):
    """Give each attempt its verdict, or None.

    An outcome belongs to the attempt whose span contains its time, else to
    the attempt that ENDED most recently before it: the operator lets go,
    then presses, so the verdict for attempt N usually lands in the pause
    before N+1 -- attaching it to the NEXT attempt would label a reach with
    the result of the one before. A second verdict for the same attempt
    overwrites the first (the operator corrected themselves); one before any
    attempt has nothing to describe and is dropped.
    """
    for a in atts:
        a["outcome"] = None
    for o in outcomes or ():
        t = o.get("t")
        if not isinstance(t, (int, float)) or not atts:
            continue
        pick = None
        for a in atts:
            if a["t0"] <= t <= a["t1"]:
                pick = a
                break
            if a["t1"] < t:
                pick = a               # the latest attempt that ended before t
        if pick is not None:
            pick["outcome"] = {"result": o.get("result"), "phase": o.get("phase") or ""}
    return atts


def check(path):
    """-> a report dict. `ok` is False when any issue is an error or a warning."""
    header, body, footer, marks, outcomes = load(path)
    issues = []

    def issue(code, severity, detail):
        issues.append({"code": code, "severity": severity, "detail": detail})

    rep = {"path": os.path.basename(str(path)), "frames": len(body),
           "arm_mode": (header or {}).get("arm_mode"),
           "has_header": header is not None, "has_footer": footer is not None,
           # A synthetic calibration programme: its
           # robot.q rows are the commanded pose, not measurements, so nothing
           # below about tracking means anything for it, and it must never be
           # counted as a demonstration.
           "synthetic": bool((header or {}).get("synthetic"))}
    if footer is None:
        issue("no_footer", "error", "no footer: the session was not stopped cleanly")
    else:
        if footer.get("frames") != len(body):
            issue("footer_mismatch", "error", f"footer says {footer.get('frames')} frames, file has {len(body)}")
        if footer.get("dropped"):
            issue("dropped_rows", "warn", f"the recorder dropped {footer['dropped']} rows")
        rep["camera_frames"] = footer.get("cam_frames")
    if header is None:
        issue("no_header", "error", "no header")
    if not body:
        issue("empty", "error", "no frames")
        rep.update(ok=False, issues=issues)
        return rep

    t = np.array([float(r.get("t", np.nan)) for r in body])
    dt = np.diff(t)
    span = float(t[-1] - t[0]) if len(t) > 1 else 0.0
    rep["span_s"] = round(span, 1)
    rep["fps"] = round(float(1.0 / np.median(dt)), 1) if len(dt) and np.median(dt) > 0 else 0.0
    if len(dt) and np.any(dt < 0):
        issue("time_not_monotonic", "error", f"{int((dt < 0).sum())} rows go backwards in time")
    gaps = dt[dt > GAP_S] if len(dt) else np.array([])
    rep["gaps"] = {"count": int(len(gaps)), "max_s": round(float(gaps.max()), 1) if len(gaps) else 0.0}
    if len(gaps):
        issue("gap", "warn", f"{len(gaps)} pause(s) over {GAP_S:g} s inside the session, longest {gaps.max():.1f} s; "
                             "split into attempts at the pauses (or mark them next time)")

    states = [(r.get("robot") or {}).get("state") for r in body]
    armed = [s == "ARMED" for s in states]
    n_armed = sum(armed)
    rep["usable_frac"] = round(n_armed / len(body), 3)
    run = best = 0
    for a in armed:
        run = run + 1 if a else 0
        best = max(best, run)
    rep["longest_armed_s"] = round(best / rep["fps"], 1) if rep["fps"] else None
    if n_armed == 0:
        issue("no_armed", "error", "no ARMED rows: nothing here is a demonstration")
    elif rep["usable_frac"] < LOW_USABLE:
        issue("low_usable", "warn", f"only {100*rep['usable_frac']:.0f} % of rows are ARMED")

    # What the onboard did with the frames it read.
    known = [(r.get("robot") or {}).get("applied") for r, a in zip(body, armed) if a]
    known = [k for k in known if k is not None]
    refusals = {}
    for r, a in zip(body, armed):
        rb = r.get("robot") or {}
        if a and rb.get("applied") is False and rb.get("refused"):
            refusals[rb["refused"]] = refusals.get(rb["refused"], 0) + 1
    rep["refusals"] = refusals
    if known:
        rep["applied_frac"] = round(sum(1 for k in known if k) / len(known), 3)
        if rep["applied_frac"] < LOW_APPLIED:
            issue("low_applied", "warn",
                  f"the onboard applied only {100*rep['applied_frac']:.0f} % of the armed frames it read"
                  + (f" (refused: {refusals})" if refusals else " (no reason recorded)"))
    else:
        rep["applied_frac"] = None

    # Measured state: how often it changed and how old it was.
    qs = [tuple(rb["q"]) for rb in ((r.get("robot") or {}) for r in body) if rb.get("q")]
    rep["q_updates_per_s"] = round(sum(1 for a, b in zip(qs, qs[1:]) if a != b) / span, 1) if span > 0 and len(qs) > 1 else None
    ages = [(r.get("robot") or {}).get("q_age_s") for r in body]
    ages = np.array([a for a in ages if isinstance(a, (int, float))])
    rep["q_age_p90_s"] = round(float(np.percentile(ages, 90)), 3) if len(ages) else None
    if len(ages) and rep["q_age_p90_s"] > STALE_STATE_S:
        issue("stale_state", "warn", f"measured joints are older than {1000*STALE_STATE_S:.0f} ms on 10 % of rows (p90 {1000*rep['q_age_p90_s']:.0f} ms)")
    # Body state: waist + arms with velocities and torque. Without dq a
    # physics replay of the recorded commands cannot be compared against what
    # the robot did, which is the one measurement of the sim-to-real gap that
    # does not depend on the simulator being right first.
    armed_rows_b = [r for r in body if (r.get("robot") or {}).get("state") == "ARMED"]
    body_rows = [r for r in armed_rows_b
                 if isinstance((r.get("robot") or {}).get("body"), dict)
                 and (r["robot"]["body"].get("dq"))]
    rep["body_rows"] = len(body_rows)
    rep["body_frac"] = round(len(body_rows) / len(armed_rows_b), 3) if armed_rows_b else None
    if armed_rows_b and not body_rows:
        issue("no_body_state", "warn",
              "no measured body state (waist+arm q/dq/tau): predates body-state recording; "
              "a physics replay cannot use dq")
    elif body_rows:
        dqs = np.array([r["robot"]["body"]["dq"] for r in body_rows], dtype=float)
        if dqs.size and float(np.abs(dqs).max()) == 0.0:
            issue("zero_dq", "warn", "body dq is exactly zero on every armed row: velocities are not being read")

    # Hands, targets, fingers.
    stale = one_side = nonfinite = 0
    lim = _limits()
    over = {}
    wrist_targets = False
    for r in body:
        hands = {h.get("hand") for h in (r.get("hands") or ())}
        tg = r.get("targets") or {}
        for s in ("left", "right"):
            if f"{s}_elbow_joint" not in tg and f"{s}_wrist_roll_joint" not in tg:
                one_side += 1
            if s not in hands and any(n in tg for n in FINGERS[s]):
                stale += 1
        for k, v in tg.items():
            if "wrist" in k:
                wrist_targets = True
            if not isinstance(v, (int, float)) or not math.isfinite(v):
                nonfinite += 1
                continue
            if k in lim:
                lo, hi = lim[k]
                if v < lo - LIMIT_TOL or v > hi + LIMIT_TOL:
                    over[k] = over.get(k, 0) + 1
    rep["stale_finger_rows"] = stale
    rep["one_side_rows"] = one_side
    rep["out_of_limit"] = over
    if stale and not rep.get("synthetic"):
        # A synthetic programme has no headset in the frame by construction;
        # its finger targets are the programme's, not a copy of a lost hand.
        issue("stale_fingers", "error", f"{stale} rows carry finger targets for a hand that is not in the frame")
    if nonfinite:
        issue("nonfinite", "error", f"{nonfinite} non-finite target values")
    if over:
        issue("out_of_limit", "warn", f"targets past a joint limit: {over}")
    armed_rows = [r for r in body if (r.get("robot") or {}).get("state") == "ARMED"]
    # What the robot was TOLD. `targets` is the operator's intent; the sink
    # subtracts the arming offset before the wire, so a file without `sent`
    # cannot be replayed into a simulator without reproducing a lurch the
    # real robot never made. Files before 2026-09-16 have none.
    rep["sent_rows"] = sum(1 for r in body if isinstance(r.get("sent"), dict))
    rep["arm_offset_rows"] = sum(1 for r in body if "arm_offset" in r)
    armed_with_targets = sum(1 for r, a in zip(body, armed) if a and r.get("targets"))
    if armed_with_targets and rep["sent_rows"] == 0:
        issue("no_sent", "warn", "ARMED rows carry targets but no `sent` (predates sent "
                                 "recording): the command the robot received is not in this "
                                 "file, only the operator's intent before the arming offset")
    # Fingers: did the knuckles keep following, and what did the hand say
    # about itself. `robot.hand_q` is measured position per side (bridge
    # order); `robot.hand_state` is temperature/mode/error per motor.
    _check_hands(armed_rows, rep, issue)

    rep["wrist_ref_rows"] = sum(1 for r in body if "wrist_ref" in r)
    if wrist_targets and rep["wrist_ref_rows"] == 0:
        issue("no_wrist_ref", "warn", "wrist targets without a wrist reference: the wrists cannot be reproduced from this file")

    # What the onboard clamped or leashed, most often.
    for key in ("clamped", "leashed"):
        cnt, n = {}, 0
        for r, a in zip(body, armed):
            lst = (r.get("robot") or {}).get(key)
            if not a or lst is None:
                continue
            n += 1
            for j in lst:
                cnt[j] = cnt.get(j, 0) + 1
        rep[f"{key}_top"] = ({j: round(c / n, 3) for j, c in sorted(cnt.items(), key=lambda kv: -kv[1])[:4]}
                             if n else None)
    rep["camera_rows"] = sum(1 for r in body if isinstance(r.get("cam"), dict))

    # Depth beside the camera frames, when a camera source supplies it.
    # Counted from the footer (the recorder
    # knows per frame whether depth came), and judged against what the camera
    # PROMISED in the header: a camera whose depth client was up and a
    # session with none is a fault worth a warning; a camera that never had a
    # depth client (older bridge, TELEOP_CAM_SOURCE=onboard) is not.
    cam_n = (footer or {}).get("cam_frames") or 0
    depth_n = (footer or {}).get("depth_frames")
    rep["depth_frames"] = depth_n
    rep["depth_frac"] = (round(depth_n / cam_n, 3) if isinstance(depth_n, int) and cam_n else None)
    cam_hdr = ((header or {}).get("camera") or {}) if isinstance((header or {}).get("camera"), dict) else {}
    depth_promised = bool(((cam_hdr.get("depth") or {}) if isinstance(cam_hdr.get("depth"), dict) else {})
                          .get("enabled"))
    if cam_n and depth_promised and not depth_n:
        issue("no_depth", "warn", "the camera reported RGB-D enabled but no frame carries depth: "
                                  "check the camera service's /frame.recorder-rgbd (423 = another reader "
                                  "holds the lease, 409 = the bridge fetched too late)")
    elif cam_n and isinstance(depth_n, int) and depth_n and (cam_n - depth_n) > DEPTH_MISSING_FRAC * cam_n:
        issue("depth_missing", "warn", f"{cam_n - depth_n} of {cam_n} camera frames have no depth "
                                       f"({100 * (cam_n - depth_n) / cam_n:.0f} % > {100 * DEPTH_MISSING_FRAC:.0f} %)")

    _check_hand_cameras(path, header, body, footer, rep, issue)

    rep["marks"] = len(marks)
    att = attempts(body, marks, outcomes=outcomes)
    rep["attempts"] = len(att)
    rep["attempt_list"] = att
    # The verdicts. An attempt without one is a warning, not an error: the
    # frames are still a recording, they are just not a labelled example.
    counts = {"success": 0, "fail": 0, "abort": 0}
    for o in outcomes:
        r = o.get("result")
        counts[r] = counts.get(r, 0) + 1
    rep["outcomes"] = counts
    unlabelled = sum(1 for a in att if a.get("outcome") is None)
    # Warn only when the operator WAS labelling: some attempts have a verdict
    # and others do not, so one was missed. A session with no outcomes at all
    # (a calibration run, a tracking test) is not a defect; its zero counts
    # in the summary say what it is.
    if unlabelled and outcomes:
        issue("no_outcome", "warn", f"{unlabelled} attempt(s) without an outcome: "
                                    "review the recording and label the missing attempt offline")

    rep["issues"] = issues
    rep["ok"] = not any(i["severity"] in ("error", "warn") for i in issues)
    return rep


HAND_CAM_FRESH_S = 0.2    # a hand frame older than this at a pose row is stale
HAND_CAM_FRESH_FRAC = 0.8 # ARMED rows below this share with a fresh frame: warn


def _hand_index(path, name):
    """The rows of <sid>.frames/hand-<name>/index.jsonl, or [] without one."""
    base = str(path)
    for suffix in (".jsonl.gz", ".jsonl"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    p = os.path.join(base + ".frames", "hand-" + name, "index.jsonl")
    try:
        with open(p) as fh:
            return [json.loads(l) for l in fh if l.strip()]
    except (OSError, ValueError):
        return []


def _check_hand_cameras(path, header, body, footer, rep, issue):
    """Per hand camera: frames written, measured fps and the largest gap
    between captures, and the share of ARMED rows that had a fresh frame.

    Expected cameras are the ones the header says were connected when the
    session started, plus any the footer counted. A camera that was expected
    and wrote nothing while the robot was ARMED is a warning: an episode
    without the wrist views looks complete until someone trains on it."""
    hdr = (header or {}).get("hand_cameras")
    expected = set(hdr) if isinstance(hdr, dict) and "error" not in hdr else set()
    counts = (footer or {}).get("hand_cams") or {}
    names = sorted(expected | set(counts))
    rep["hand_cameras"] = {}
    if not names:
        return
    armed = [r for r in body if str((r.get("robot") or {}).get("state") or "").startswith("ARMED")]
    for name in names:
        c = counts.get(name) or {}
        idx = _hand_index(path, name)
        t = [r["t_cap_mono_ns"] for r in idx if isinstance(r.get("t_cap_mono_ns"), int)]
        gaps = [(b - a) / 1e9 for a, b in zip(t, t[1:]) if b >= a]
        fresh = sum(1 for r in armed
                    if ((r.get("hand_cams") or {}).get(name) or {}).get("age_s", 1e9) <= HAND_CAM_FRESH_S)
        rep["hand_cameras"][name] = {
            "frames": c.get("frames", len(idx)), "dropped": c.get("dropped", 0),
            "fps": (round((len(t) - 1) / ((t[-1] - t[0]) / 1e9), 1)
                    if len(t) > 1 and t[-1] > t[0] else None),
            "max_gap_s": round(max(gaps), 3) if gaps else None,
            "armed_rows": len(armed), "armed_rows_fresh": fresh}
        if c.get("dropped"):
            issue("hand_cam_dropped", "warn",
                  f"{name} hand camera: the recorder dropped {c['dropped']} frames")
        if armed and not c.get("frames"):
            issue("hand_cam_empty", "warn",
                  f"{name} hand camera wrote no frames while the robot was ARMED")
        elif armed and fresh < HAND_CAM_FRESH_FRAC * len(armed):
            issue("hand_cam_stale", "warn",
                  f"{name} hand camera: only {fresh} of {len(armed)} ARMED rows had a frame "
                  f"newer than {HAND_CAM_FRESH_S} s")


HAND_ORDER = ("thumb_0", "thumb_1", "thumb_2", "middle_0", "middle_1", "index_0", "index_1")


def hand_follow_thirds(armed_rows, side, joint):
    """-> [first, middle, last] follow % for one finger joint, or None.

    Follow % is measured travel over commanded travel within each third of
    the ARMED rows. The number that separated the knuckles (98 -> 17 -> 12)
    from the outer joints (99 -> 98 -> 98) in the handfix session.
    """
    i = HAND_ORDER.index(joint)
    name = f"{side}_hand_{joint}_joint"
    pairs = []
    for r in armed_rows:
        hq = ((r.get("robot") or {}).get("hand_q") or {}).get(side)
        c = (r.get("targets") or {}).get(name)
        if not isinstance(hq, list) or len(hq) != 7 or not isinstance(c, (int, float)):
            continue
        m = hq[i]
        if isinstance(m, (int, float)):
            pairs.append((float(c), float(m)))
    if len(pairs) < DERATE_MIN_ROWS:
        return None
    n = len(pairs) // 3
    out = []
    for k in range(3):
        seg = pairs[k * n:(k + 1) * n] if k < 2 else pairs[2 * n:]
        cs = np.array([c for c, _ in seg]); ms = np.array([m for _, m in seg])
        csw = float(cs.max() - cs.min())
        if csw < 0.2:                     # the operator barely moved it: no verdict
            out.append(None)
            continue
        out.append(round(100.0 * float(ms.max() - ms.min()) / csw, 1))
    return out


def _check_hands(armed_rows, rep, issue):
    derate = {}
    thirds = {}
    for side in ("left", "right"):
        for joint in HAND_ORDER:
            f = hand_follow_thirds(armed_rows, side, joint)
            if f is None:
                continue
            thirds[f"{side}_hand_{joint}_joint"] = f
            if (joint.endswith("_0") and f[0] is not None and f[2] is not None
                    and f[0] > 0 and f[2] < DERATE_RATIO * f[0]):
                derate[f"{side}_hand_{joint}_joint"] = f
    rep["hand_follow_thirds"] = thirds
    if derate:
        detail = "; ".join(f"{n} {f[0]:.0f} % -> {f[1] if f[1] is not None else '?'} % -> {f[2]:.0f} %"
                           for n, f in derate.items())
        issue("hand_derate", "warn",
              f"knuckle follow collapsed over the session ({detail}). The joint stopped doing "
              "what it was told and did not recover: read hand_state temperature/motorstate "
              "for why (DEBUG-FINGERS.md)")
    temps = []
    errors = 0
    for r in armed_rows:
        hs = (r.get("robot") or {}).get("hand_state")
        if not isinstance(hs, dict):
            continue
        for s in hs.values():
            if not isinstance(s, dict):
                continue
            temps.extend(v for v in (s.get("temperature") or []) if isinstance(v, (int, float)))
            if any(s.get("motorstate") or []) or any(s.get("error") or []):
                errors += 1
    rep["hand_temp_max_c"] = max(temps) if temps else None
    rep["hand_error_rows"] = errors
    if temps and rep["hand_temp_max_c"] >= HOT_C:
        issue("hand_hot", "warn", f"a Dex3 motor reached {rep['hand_temp_max_c']} C: a knuckle "
                                  "that stopped following while hot is derating, not latched")
    if errors:
        issue("hand_error", "error", f"{errors} armed row(s) with a non-zero Dex3 motorstate or "
                                     "hand error word: the hand reported a fault")


def summary(rep):
    lines = [f"{rep['path']}: {'OK' if rep.get('ok') else 'ISSUES'}  "
             f"{'SYNTHETIC programme (not a demonstration)  ' if rep.get('synthetic') else ''}"
             f"{rep.get('frames')} frames, {rep.get('span_s')} s, {rep.get('fps')} fps, "
             f"ARMED {100*(rep.get('usable_frac') or 0):.0f} % (longest run {rep.get('longest_armed_s')} s), "
             f"applied {('%.0f %%' % (100*rep['applied_frac'])) if rep.get('applied_frac') is not None else 'n/a'}, "
             f"q {rep.get('q_updates_per_s')}/s age p90 {rep.get('q_age_p90_s')} s, "
             f"camera rows {rep.get('camera_rows')}"
             + (f", body {100*rep['body_frac']:.0f} %" if rep.get('body_frac') is not None else "")
             + (f", depth {100*rep['depth_frac']:.0f} %" if rep.get('depth_frac') is not None else "")
             + (f", hand max {rep['hand_temp_max_c']} C" if rep.get('hand_temp_max_c') is not None else "")
             + f", attempts {rep.get('attempts')} ({rep.get('marks')} marked), "
             + "outcomes " + (", ".join(f"{k} {v}" for k, v in (rep.get("outcomes") or {}).items()) or "none")]
    for i in rep.get("issues", []):
        lines.append(f"  [{i['severity']}] {i['code']}: {i['detail']}")
    return "\n".join(lines)
