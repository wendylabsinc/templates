"""The per-joint finger range must be reported even when no arm is driven.

Fingers-only mode (TELEOP_FINGERS_ONLY, `take_hands`) puts no arm joints on
the wire at all. In that mode the finger range in /control/status is the only
number that says whether the hands are following -- and it was exactly the
mode in which the number vanished: _note_targets returned early when the frame
carried no arm joints, before it had recorded the fingers.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))
import server  # noqa: E402


def _finger_frame(t):
    """Both hands, every Dex3 joint, sweeping a fraction `t` of its range."""
    out = {}
    for joints in server._DEX3.values():
        for name, lo, hi in joints:
            out[name] = lo + t * (hi - lo)
    return out


def test_finger_range_is_reported_when_no_arm_joint_is_present():
    stats = {}
    server._note_targets(stats, _finger_frame(0.1))
    server._note_targets(stats, _finger_frame(0.6))

    assert stats["arm_targets"] == 0, "no arm joints were sent"
    fingers = stats.get("fingers")
    assert fingers, "finger range missing in the one mode where it is the only signal"
    assert len(fingers) == 14
    for entry in fingers.values():
        assert entry["used_pct"] == 50.0
        assert "limit" in entry and "at_min" in entry and "at_max" in entry


def test_finger_range_is_still_reported_alongside_arm_joints():
    stats = {}
    frame = dict(_finger_frame(0.0), left_elbow_joint=0.1)
    server._note_targets(stats, frame)
    server._note_targets(stats, dict(_finger_frame(1.0), left_elbow_joint=0.2))
    assert stats["arm_targets"] == 1
    assert stats["fingers"] and all(e["used_pct"] == 100.0 for e in stats["fingers"].values())
