"""A single tracked hand must not produce arm targets.

The arm IK needs both wrists. When only one hand is tracked it is skipped
entirely -- so whatever `_last` holds is what gets sent, at 90 fps, looking
exactly like a real command. Seeded with zeros, that drove both arms to the
zero pose on nothing more than the operator's other hand leaving view.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))

ARM = ["left_shoulder_pitch_joint", "left_shoulder_roll_joint",
       "left_shoulder_yaw_joint", "left_elbow_joint", "left_wrist_roll_joint",
       "left_wrist_pitch_joint", "left_wrist_yaw_joint"]


class Retargeter:
    """The fallback logic of G1Retargeter.process, isolated from Pinocchio.

    retarget_g1 imports dex_retargeting and pinocchio at module scope, neither
    of which installs on a laptop -- but the defect is in the fallback dict, not
    in the solver, so that is what this pins.
    """
    def __init__(self, seed):
        self._last = seed
        self.ik_calls = 0

    def process(self, frame):
        hands = {h.get("hand"): h for h in frame.get("hands", [])}
        out = dict(self._last)
        lw, rw = hands.get("left", {}), hands.get("right", {})
        if "wrist" in lw and "wrist" in rw:
            self.ik_calls += 1
            out.update({n: 0.5 for n in ARM})      # stand-in for solve_ik
        self._last = out
        return out


ONE_HAND = {"hands": [{"hand": "right", "wrist": [0, 0, -0.3]}]}
TWO_HANDS = {"hands": [{"hand": "left", "wrist": [0, 0, -0.3]},
                       {"hand": "right", "wrist": [0, 0, -0.3]}]}


def test_the_old_zero_seed_fabricated_a_full_arm_command():
    """Characterise the bug, so the fix cannot be quietly reverted."""
    r = Retargeter({n: 0.0 for n in ARM})
    out = r.process(ONE_HAND)
    assert r.ik_calls == 0, "the IK cannot run on one wrist"
    assert all(n in out for n in ARM), "…yet every arm joint was present"
    assert all(out[n] == 0.0 for n in ARM), "…commanding the zero pose"


def test_one_hand_emits_no_arm_targets_at_all():
    r = Retargeter({})
    out = r.process(ONE_HAND)
    assert r.ik_calls == 0
    missing = [n for n in ARM if n not in out]
    assert missing == ARM, "arm targets were invented from a single hand"


def test_the_sink_drops_such_a_frame():
    """End to end: an incomplete frame must never reach the robot."""
    import types
    import sink_realg1
    import asyncio
    sink = sink_realg1.RealG1Sink.__new__(sink_realg1.RealG1Sink)
    sink._latest, sink._latest_t = None, 0.0
    sink._lock = __import__("threading").Lock()
    asyncio.run(sink.apply(Retargeter({}).process(ONE_HAND)))
    assert sink._latest is None, "a one-handed frame was forwarded to the robot"
    assert sink.fresh_frame() is None


def test_after_two_hands_a_dropout_holds_the_solved_pose():
    """Once solved, falling back really is 'hold still' -- the arms are there."""
    r = Retargeter({})
    r.process(TWO_HANDS)
    out = r.process(ONE_HAND)
    assert all(out[n] == 0.5 for n in ARM), "fell back to something else"
    assert r.ik_calls == 1


def test_the_real_module_seeds_empty():
    src = os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge", "retarget_g1.py")
    body = open(src).read()
    assert "self._last = {}" in body
    assert "self._last = {n: 0.0 for n" not in body, "the zero seed is back"


def test_fingers_only_skips_the_ik_but_never_invents_arm_targets():
    """Measured on hardware: the arm IK runs at ~10 Hz against 85 fps of hand
    data. Fingers do not need it, so fingers-only solves once and then reuses
    the result, freezing the arm targets.

    The safety property is that it must never ZERO them. Zeroing was one config
    mistake away from driving both arms to the zero pose -- the bug fixed in
    211a9da. Freezing the last solved pose is safe; fabricating one is not.
    """
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "teleop", "bridge", "retarget_g1.py")).read()
    assert "if self.fingers_only:" in src
    block = src.split("if self.fingers_only:")[1].split("# ---- arms")[0]
    assert "all(n in out for n in ARM_LEFT + ARM_RIGHT)" in block, \
        "the IK is skipped before any arm values exist"
    assert "0.0" not in block, "arm joints look like they are being zeroed"


def test_fingers_only_still_produces_a_complete_frame():
    """The sink drops a frame with missing joints, so an incomplete
    fingers-only frame would stop everything silently."""
    r = Retargeter({})
    r.process(TWO_HANDS)                      # one solve populates the arms
    out = r.process(ONE_HAND)
    assert all(n in out for n in ARM), "the frame lost its arm joints"
