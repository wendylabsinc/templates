"""The retargeter must aim somewhere the arm can reach.

This is the check that was missing. Everything else verified transport --
frames arrive, joints obey commands -- and all of it passed while the mapping
aimed 0.84 m below the robot's feet. An earlier self-test asserted only that
joints "moved > 0.02 rad" between two hand poses, which a fully saturated
solver clears trivially.

Workspace numbers come from sampling the 7-DoF chain in
assets/g1/g1_body29_hand14.urdf inside its joint limits (20k samples):
shoulder at (0, +-0.100, 0.292) in the pelvis frame, max reach 0.459 m.
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))

# Duplicated rather than imported: retarget_g1 pulls in dex_retargeting and
# pinocchio at module scope, neither of which installs on a laptop. The
# constants are asserted against the module's own text below so they cannot
# drift apart silently.
_R_XR_TO_ROBOT = np.array([[0.0, 0.0, -1.0], [-1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
_PELVIS_TO_HEAD = np.array([0.054, 0.0, 0.474])
_REACH_SCALE = 0.74
_SHOULDER = {"left": np.array([0.0, 0.100, 0.292]),
             "right": np.array([0.0, -0.100, 0.292])}
_MAX_REACH = 0.459
_BOX = {"x": (-0.43, 0.43), "y": (-0.28, 0.55), "z": (-0.12, 0.73)}


def target(pos_head, scale=_REACH_SCALE, offset=_PELVIS_TO_HEAD):
    return _R_XR_TO_ROBOT @ (np.asarray(pos_head) * scale) + offset


# Hand positions relative to the head, in the WebXR frame: X right, Y up,
# -Z forward. These are poses a person actually adopts while teleoperating.
POSES = {
    "chest height, forward":   (-0.20, -0.45, -0.35),
    "face height, forward":    (-0.20, 0.00, -0.35),
    "hands at sides":          (-0.25, -0.70, -0.10),
    "reaching for a can":      (-0.15, -0.40, -0.45),
    "hands together, centre":  (-0.02, -0.40, -0.35),
    "arms out to the side":    (-0.45, -0.30, -0.15),
}


@pytest.mark.parametrize("name,pos", POSES.items())
def test_ordinary_hand_poses_are_reachable(name, pos):
    t = target(pos)
    d = float(np.linalg.norm(t - _SHOULDER["left"]))
    assert d <= _MAX_REACH, (
        f"{name}: target {t.round(3)} is {d:.3f} m from the shoulder, "
        f"beyond the {_MAX_REACH} m reach — the IK will saturate and the arm "
        f"will not follow the operator")


@pytest.mark.parametrize("name,pos", POSES.items())
def test_ordinary_hand_poses_land_inside_the_workspace_box(name, pos):
    t = target(pos)
    for axis, v in zip("xyz", t):
        lo, hi = _BOX[axis]
        assert lo <= v <= hi, f"{name}: {axis}={v:+.3f} outside {lo}..{hi}"


def test_the_old_constants_put_every_pose_out_of_reach():
    """Characterise the bug, so a revert cannot pass silently."""
    bad_offset, bad_scale = np.array([0.0, 0.0, -0.35]), 1.1
    for name, pos in POSES.items():
        t = target(pos, scale=bad_scale, offset=bad_offset)
        d = float(np.linalg.norm(t - _SHOULDER["left"]))
        assert d > _MAX_REACH, f"{name} was reachable under the old constants"


def test_the_offset_points_upward():
    """The IK solves in the PELVIS frame; the head is above the pelvis."""
    assert _PELVIS_TO_HEAD[2] > 0, "head offset points down again"
    assert _PELVIS_TO_HEAD[2] > 0.292, "head is below the shoulders"


def test_the_scale_shrinks_human_motion():
    """The robot's arm is shorter than the operator's."""
    assert _REACH_SCALE < 1.0, "scaling a 0.62 m human reach UP onto a 0.459 m arm"
    assert 0.6 < _REACH_SCALE < 0.9


def test_the_module_still_uses_these_constants():
    """Guard the duplication above."""
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "teleop", "bridge", "retarget_g1.py")).read()
    assert "_PELVIS_TO_HEAD = np.array([0.054, 0.0, 0.474])" in src
    assert "_REACH_SCALE = 0.74" in src
    # The old name may appear in the comment that explains the error; what
    # must not come back is a line of CODE using it.
    code = [l for l in src.splitlines()
            if "_HEAD_TO_TORSO" in l and not l.lstrip().startswith("#")]
    assert not code, f"the old head->pelvis constant is back in use: {code}"


def test_saturation_is_reported():
    """An out-of-reach mapping pins joints to their bounds every frame.

    Nothing surfaced that, so a saturated solver looked identical to a healthy
    one from outside.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "onboard"))
    import g1_onboard as ob
    ob.sanitize_arm([0.0] * 14)
    assert ob.LAST_CLAMPED["n"] == 0, "a neutral pose should clamp nothing"
    ob.sanitize_arm([99.0] * 14)
    assert ob.LAST_CLAMPED["n"] == 14, "every joint was out of range and unreported"
    assert ob.LAST_CLAMPED["joints"] == list(range(14))


# --------------------------------------------------------------------------- #
# the control endpoints were open to the whole office LAN
# --------------------------------------------------------------------------- #
def test_control_requires_a_key_when_one_is_set():
    """The onboard has had a shared key since it existed; the bridge endpoint
    that DRIVES it never did. Anything reaching the port could arm the robot."""
    import types
    import server

    def req(app, query=None, header=None):
        return types.SimpleNamespace(
            app=app, query=query or {},
            headers=header or {})

    assert server._control_allowed(req({"control_key": ""})) is True, \
        "unset key must not lock an operator out"
    app = {"control_key": "s3cret"}
    assert server._control_allowed(req(app)) is False, "no key was accepted"
    assert server._control_allowed(req(app, {"key": "wrong"})) is False
    assert server._control_allowed(req(app, {"key": "s3cret"})) is True
    assert server._control_allowed(req(app, header={"X-Teleop-Key": "s3cret"})) is True


def _cmd():
    import threading
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "onboard"))
    import g1_onboard as ob
    c = ob.Commander.__new__(ob.Commander)
    c.lock = threading.RLock()
    c._rx_age = []
    c._rx_min = None
    c._rx_min_proc = None
    c._rx_baseline_state = "STANDBY"
    c.state = "STANDBY"
    return c


def test_a_saturated_backlog_is_not_reported_as_a_slow_reader():
    """The regime the next session will actually be in.

    A backlog does not grow forever: once the socket buffers fill the sender
    blocks and the queue sits at a constant depth. Drift then reads ~0 and a
    drift-only verdict says "no backlog, slow reader" -- which recommends
    splitting the process when the fix is a latest-wins drain.
    """
    c = _cmd()
    c.note_rx_age(0.02)             # the connection starts un-queued
    for _ in range(60):
        c.note_rx_age(3.0)          # then the queue saturates: constant, late
    t = c.rx_age_trend()
    assert t["drift_s"] < 0.25, "drift alone stays under the threshold — it cannot see this"
    assert "SATURATED" in t["verdict"], t["verdict"]
    assert "F5" in t["verdict"], "pointed at the wrong fix"


def test_a_genuinely_slow_reader_still_reads_as_D1():
    c = _cmd()
    for i in range(60):
        c.note_rx_age(0.02 + (i % 3) * 0.005)    # small, no queue
    t = c.rx_age_trend()
    assert t["excess_s"] < 0.1
    assert "D1" in t["verdict"], t["verdict"]


def test_a_constant_clock_skew_does_not_fake_a_backlog():
    """min_s absorbs the offset; excess is measured against it."""
    c = _cmd()
    for i in range(60):
        c.note_rx_age(12.0 + (i % 3) * 0.005)    # absurd but constant skew
    t = c.rx_age_trend()
    assert t["min_s"] == pytest.approx(12.0, abs=0.01)
    assert t["excess_s"] < 0.1
    assert "D1" in t["verdict"], t["verdict"]


def test_the_window_reports_its_duration_not_just_its_length():
    """At 4 frames/s a 400-sample window spans 100 s; "flat" over what?"""
    c = _cmd()
    for _ in range(30):
        c.note_rx_age(0.05)
    assert "window_s" in c.rx_age_trend()


def test_reconnecting_clears_the_minimum():
    """A fresh socket is a fresh queue; the old minimum would understate the
    offset and read as excess that is not there."""
    c = _cmd()
    for _ in range(60):
        c.note_rx_age(0.01)
    c.reset_rx_age()
    assert c.rx_age_trend() is None


def test_rx_age_trend_separates_a_slow_reader_from_a_backlog():
    """A slow read loop and a queuing socket look identical in every other
    counter and need opposite fixes."""
    c = _cmd()
    assert c.rx_age_trend() is None, "reported a trend with no data"

    for _ in range(60):                      # constant lateness = slow reader
        c.note_rx_age(0.30)
    assert "D1" in c.rx_age_trend()["verdict"]

    c._rx_age = []; c._rx_min = None; c._rx_min_proc = None
    for i in range(60):                      # growing lateness = a queue
        c.note_rx_age(0.05 + i * 0.03)
    t = c.rx_age_trend()
    assert "GROWING BACKLOG" in t["verdict"], t
    assert t["drift_s"] > 0.25

    # a constant clock offset between the two boxes must not fake a backlog
    c._rx_age = []; c._rx_min = None; c._rx_min_proc = None
    for _ in range(60):
        c.note_rx_age(12.0)                  # absurd skew, but constant
    assert "D1" in c.rx_age_trend()["verdict"]



def test_a_reconnect_under_load_does_not_hide_the_backlog():
    """The per-connection minimum is only a clock offset if the first frames
    of the connection are read un-queued. The sink retries every 2 s, so it
    can reconnect mid-session with the queue already saturated -- and then its
    own minimum is biased high and excess_s understates.

    The clock offset does not change on reconnect; only the queue does. So the
    process-lifetime minimum from an earlier, healthy connection is still a
    valid baseline.
    """
    c = _cmd()
    c.note_rx_age(0.02)                  # a healthy earlier connection
    for _ in range(30):
        c.note_rx_age(0.03)

    c.state = "ARMED"
    c.reset_rx_age()                     # sink drops and reconnects, under load
    for _ in range(60):
        c.note_rx_age(3.0)               # every read already late

    t = c.rx_age_trend()
    assert t["min_connection_s"] == pytest.approx(3.0, abs=0.01), \
        "this connection never saw an un-queued frame"
    assert t["min_process_s"] == pytest.approx(0.02, abs=0.01), \
        "the earlier connection's offset was lost"
    assert "SATURATED" in t["verdict"], \
        f"reconnect-under-load hid the backlog: {t['verdict']}"
    assert "ARMED" in t["baseline_caveat"], "the caveat was not surfaced"


def test_a_clean_standby_baseline_carries_no_caveat():
    c = _cmd()
    for _ in range(30):
        c.note_rx_age(0.03)
    assert c.rx_age_trend()["baseline_caveat"] == ""
