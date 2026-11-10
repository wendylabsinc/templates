"""Three defects found by validating the real can sessions row by row.

1. An untracked hand's finger targets were copied from the last frame into
   every row while the hand was missing -- 3226 rows in one controller run,
   unmarked. A policy trained on that learns that a hand you cannot see keeps
   commanding its last grip. The retargeter now emits fingers only for hands
   in the frame; the sink holds the last sent grip for the missing hand so
   the tracked hand keeps working; the recording is truthful.

2. The onboard's ack said whether it applied a frame but not why it refused
   one. In the two hand-tracking can sessions only 37 % and 41 % of armed rows
   were applied, and the file could not say which of four reasons. Now it can.

3. Nothing checked a session file when it was written. The validator that
   found the two problems above runs at session stop, on the bridge, and its
   verdict comes back in the stop reply.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import sys
import threading
import time
import types

import numpy as np
import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "teleop", "bridge"))
sys.path.insert(0, os.path.join(ROOT, "onboard"))

import retarget_g1 as rg  # noqa: E402
import sink_realg1 as sk  # noqa: E402
import g1_onboard as ob  # noqa: E402
import episode_check  # noqa: E402
from tests.test_onboard_safety import armed_commander  # noqa: E402


# ---- 1. no stale fingers ---------------------------------------------------------

def _geometry_retargeter():
    r = rg.G1Retargeter.__new__(rg.G1Retargeter)
    r._last, r._cal_rec, r._cal = {}, None, None
    r._wrist_prev, r._gap_runs, r._wrist_ref, r._forearm_ref = {}, {}, {}, {}
    r.forearm, r.shoulder, r.wrist_fk, r.fingers_only = True, False, False, False
    r.smooth_mode = "off"
    r._finger_q = lambda landmarks, *a: [0.1] * 7           # fingers without dex_retargeting
    for a in ("_li", "_ri", "_left", "_right", "_l_to_hw", "_r_to_hw", "_rot_l", "_rot_r"):
        setattr(r, a, None)
    return r


def _hand(side):
    sh = rg.G1Retargeter._SHOULDER[side]
    inv = np.linalg.inv(rg._R_XR_TO_ROBOT)
    return {"hand": side, "landmarks": [0.01] * 75, "quat": [0.0, 0.0, 0.0, 1.0],
            "wrist": (sh + 0.27 * (inv @ np.array([1.0, 0.0, -0.4]))).tolist(), "fabricated": 0}


def test_fingers_are_emitted_only_for_hands_in_the_frame():
    r = _geometry_retargeter()
    both = r.process({"hands": [_hand("left"), _hand("right")]})
    assert all(n in both for n in rg.DEX3_LEFT + rg.DEX3_RIGHT)
    right_only = r.process({"hands": [_hand("right")]})
    assert all(n in right_only for n in rg.DEX3_RIGHT)
    assert not any(n in right_only for n in rg.DEX3_LEFT), \
        "the left hand is not in the frame; its fingers must not be copied from the last one"
    assert not any(n.startswith("left_") and "hand" not in n for n in right_only)


def test_a_frame_with_no_hands_emits_no_fingers_at_all():
    r = _geometry_retargeter()
    r.process({"hands": [_hand("left"), _hand("right")]})
    assert not any("hand" in n for n in r.process({"hands": []}))


def test_fingers_only_mode_also_drops_the_absent_hand():
    r = _geometry_retargeter()
    r.forearm, r.fingers_only = False, True
    r._last = {n: 0.0 for n in rg.ARM_LEFT + rg.ARM_RIGHT}
    out = r.process({"hands": [_hand("left")]})
    assert all(n in out for n in rg.DEX3_LEFT) and not any(n in out for n in rg.DEX3_RIGHT)


def _sink():
    s = sk.RealG1Sink.__new__(sk.RealG1Sink)
    s._latest, s._latest_t = None, 0.0
    s._lock = threading.Lock()
    s.n_sent = s.n_retargeted = 0
    s._last_hands = None
    return s


def _targets(arm_names, fingers=("left", "right"), value=0.2):
    t = {n: value for n in arm_names}
    for side in fingers:
        for n in (rg.DEX3_LEFT if side == "left" else rg.DEX3_RIGHT):
            t[n] = 0.3 if side == "left" else 0.4
    return t


def test_the_sink_holds_the_last_grip_for_a_hand_that_is_not_in_the_frame():
    s = _sink()
    right = sorted(sk.RealG1Sink._FOREARM_SET["right"] | sk.RealG1Sink._SHOULDER_SET["right"])
    asyncio.run(s.apply(_targets(right, fingers=("left", "right"))))
    asyncio.run(s.apply(_targets(right, fingers=("right",))))          # left hand gone
    assert s._latest is not None, "a one-handed frame must still drive the tracked hand"
    assert s._latest["hand_left"] == pytest.approx([0.3] * 7), "hold the last left grip"
    assert s._latest["hand_right"] == pytest.approx([0.4] * 7)


def test_a_hand_never_seen_is_sent_open_not_dropped():
    s = _sink()
    right = sorted(sk.RealG1Sink._FOREARM_SET["right"] | sk.RealG1Sink._SHOULDER_SET["right"])
    asyncio.run(s.apply(_targets(right, fingers=("right",))))
    assert s._latest is not None
    assert s._latest["hand_left"] == pytest.approx([0.0] * 7)


def test_a_hand_never_commanded_is_seeded_from_its_measured_pose():
    s = _sink()
    s.last_hand_q = {"left": [0.31] * 7, "right": [0.42] * 7}
    right = sorted(sk.RealG1Sink._FOREARM_SET["right"] | sk.RealG1Sink._SHOULDER_SET["right"])
    asyncio.run(s.apply(_targets(right, fingers=())))
    assert s._latest["hand_left"] == pytest.approx([0.31] * 7)
    assert s._latest["hand_right"] == pytest.approx([0.42] * 7)


def test_a_hand_missing_three_of_seven_fingers_is_still_refused():
    """Whole set or nothing, for fingers as for arms: three of seven is a
    frame that lost joints in transit, not a hand that is absent."""
    s = _sink()
    right = sorted(sk.RealG1Sink._FOREARM_SET["right"] | sk.RealG1Sink._SHOULDER_SET["right"])
    t = _targets(right, fingers=("left", "right"))
    for n in rg.DEX3_LEFT[:3]:
        del t[n]
    asyncio.run(s.apply(t))
    assert s._latest is None


# ---- 2. the refusal reason ----------------------------------------------------------

def test_apply_records_why_it_refused():
    c = armed_commander()
    c.measured_arm_q = [0.0] * ob.ARM_DIM
    assert c.apply([0.0] * 14, [0.0] * 7, [0.0] * 7) is True and c.last_refusal is None
    assert c.apply([0.0] * 14, [0.0] * 7, [0.0] * 7) is False and c.last_refusal == "rate_limit"
    c.last_apply_t = 0.0
    c.state = "HOLDING"
    assert c.apply([0.0] * 14, [0.0] * 7, [0.0] * 7) is False and c.last_refusal == "not_armed"
    c.state = "ARMED"
    c.last_apply_t = 0.0
    c.deadman_ok = lambda: False
    assert c.apply([0.0] * 14, [0.0] * 7, [0.0] * 7) is False and c.last_refusal == "deadman"


def test_the_ack_carries_the_refusal_and_none_when_applied():
    c = armed_commander()
    c.measured_arm_q = [0.0] * ob.ARM_DIM
    ok = c.apply([0.0] * 14, [0.0] * 7, [0.0] * 7)
    assert ob.ack_message(c, 1, ok, [])["refused"] is None
    again = c.apply([0.0] * 14, [0.0] * 7, [0.0] * 7)
    assert ob.ack_message(c, 2, again, [])["refused"] == "rate_limit"


def test_the_sink_view_carries_the_refusal():
    class _Msg:
        def __init__(self, d):
            import aiohttp
            self.type, self.data = aiohttp.WSMsgType.TEXT, json.dumps(d)

    class _Stream:
        def __init__(self, msgs):
            self._it = iter(msgs)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return _Msg(next(self._it))
            except StopIteration:
                raise StopAsyncIteration
    s = _sink()
    s.onboard_state, s.onboard_msg, s.onboard_q, s.onboard_q_t = "?", "", None, 0.0
    s.n_acked = s.n_applied_ack = 0
    s.last_ack = None
    asyncio.run(s._read(_Stream([{"ack": 3, "state": "ARMED", "applied": False, "refused": "rate_limit",
                                  "q": [0.0] * 14, "clamped": [], "leashed": []}])))
    assert s.robot_view()["refused"] == "rate_limit" and s.robot_view()["applied"] is False


# ---- 3. the validator -----------------------------------------------------------------

def _session(tmp_path, rows, gz=False, footer=True, header=None):
    sid = "20260916-000000-t"
    p = tmp_path / (f"{sid}.jsonl" + (".gz" if gz else ""))
    op = gzip.open if gz else open
    with op(p, "wt") as fh:
        fh.write(json.dumps(header or {"type": "header", "session": sid, "t_start": 100.0, "v": 1,
                                       "arm_mode": "geometry"}) + "\n")
        for r in rows:
            fh.write(json.dumps(r) + "\n")
        if footer:
            fh.write(json.dumps({"type": "footer", "frames": len(rows), "dropped": 0, "t_end": 200.0}) + "\n")
    return str(p)


def _row(i, sides=("left", "right"), armed=True, applied=True, stale_fingers_for=(), wrist_ref=True):
    hands = [{"hand": s, "landmarks": [0.01] * 75, "wrist": [0.1, -0.2, -0.3], "quat": [0, 0, 0, 1], "fabricated": 0} for s in sides]
    targets = {}
    for s in sides:
        for j in ("elbow", "shoulder_pitch", "shoulder_roll", "wrist_roll", "wrist_pitch", "wrist_yaw"):
            targets[f"{s}_{j}_joint"] = 0.1
    for s in tuple(sides) + tuple(stale_fingers_for):
        for n in (rg.DEX3_LEFT if s == "left" else rg.DEX3_RIGHT):
            targets[n] = 0.0                  # inside every Dex3 range, mirrored or not
    row = {"t": 100.0 + i / 90.0, "hands": hands, "head": {"pos": [0, 1.6, 0], "quat": [0, 0, 0, 1]},
           "robot": {"state": "ARMED" if armed else "HOLDING", "q": [0.01 * (i // 3)] * 14,
                     "q_age_s": 0.01, "ack": i, "applied": applied, "refused": None if applied else "rate_limit",
                     "clamped": [], "leashed": [],
                     # A clean session carries the measured body -- waist +
                     # arms with dq/tau -- on every row, because that is the
                     # half of a physics replay the robot supplies. Without it
                     # the check says no_body_state, on purpose.
                     "body": {"idx": list(range(12, 29)), "q": [0.01 * (i // 3)] * 17,
                              "dq": [0.3 if i % 2 else -0.3] * 17, "tau": [0.5] * 17,
                              "tick": i, "age_s": 0.01}},
           # A clean session has the can in it: a fresh tracked target on
           # every armed row. Without it the check says no_can, on purpose.
           "can": {"healthy": True, "frame_id": 1000 + i // 6, "captured_at_unix_ns": 0,
                   "frame_w": 640, "frame_h": 480, "status": "locked", "target_id": "coke-target-0001",
                   "target_fresh": True, "box_xyxy": [100.0, 120.0, 160.0, 260.0],
                   "center_normalized": [0.203, 0.396], "confidence": 0.9, "eligible": True,
                   "age_s": 0.03},
           "targets": targets}
    # A clean session says what the robot was TOLD, not only what the
    # operator meant: `sent` is the arm command after the arming offset.
    # Without it the check says no_sent, on purpose.
    row["sent"] = {j: v - 0.05 for j, v in targets.items() if "_hand_" not in j}
    if i == 0:
        row["arm_offset"] = {"v": 1, "offset": {j: 0.05 for j in row["sent"]}, "captured_at": 100.0}
    if i == 0 and wrist_ref:
        row["wrist_ref"] = {"v": 1, "sides": {}}
    return row


def test_a_clean_session_passes(tmp_path):
    rep = episode_check.check(_session(tmp_path, [_row(i) for i in range(300)]))
    assert rep["ok"] is True and rep["issues"] == [], rep
    assert rep["frames"] == 300 and rep["usable_frac"] == pytest.approx(1.0)
    assert rep["applied_frac"] == pytest.approx(1.0)
    assert rep["fps"] > 80


def test_the_validator_names_the_defects(tmp_path):
    rows = [_row(i) for i in range(100)]
    rows += [_row(100 + i, sides=("right",), stale_fingers_for=("left",)) for i in range(50)]   # stale left fingers
    rows += [_row(150 + i, applied=False) for i in range(150)]                                     # mostly refused
    rows[200]["t"] += 3.0                                                                          # a 3 s hole
    for r in rows[200:]:
        r["t"] += 3.0
    rep = episode_check.check(_session(tmp_path, rows))
    codes = {i["code"] for i in rep["issues"]}
    assert "stale_fingers" in codes
    assert "low_applied" in codes
    assert "gap" in codes
    assert rep["ok"] is False
    assert rep["refusals"] == {"rate_limit": 150}


def test_the_validator_reads_gzip_and_flags_a_missing_footer(tmp_path):
    rep = episode_check.check(_session(tmp_path, [_row(i) for i in range(50)], gz=True, footer=False))
    assert "no_footer" in {i["code"] for i in rep["issues"]}
    assert rep["frames"] == 50


def test_the_validator_flags_no_armed_frames_and_missing_wrist_ref(tmp_path):
    rep = episode_check.check(_session(tmp_path, [_row(i, armed=False, wrist_ref=False) for i in range(50)]))
    codes = {i["code"] for i in rep["issues"]}
    assert "no_armed" in codes and "no_wrist_ref" in codes


def test_session_stop_returns_the_check(tmp_path):
    import server
    from tests.test_endpoint_gating import _req, KEY
    path = _session(tmp_path, [_row(i) for i in range(120)])

    class Rec:
        def stop(self):
            return {"ok": True, "session": "20260916-000000-t", "frames": 120}

        def path(self, sid):
            return path
    app = {"control_key": KEY, "session": Rec(), "retargeter": object(), "stats": {}, "sink": server.LogSink()}
    resp = asyncio.run(server.session_handler(_req(app, {"step": "stop"}, key=KEY)))
    body = json.loads(resp.text)
    assert body["ok"] is True and body["check"]["ok"] is True and body["check"]["frames"] == 120

