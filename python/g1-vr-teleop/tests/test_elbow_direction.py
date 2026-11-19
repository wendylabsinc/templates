"""Does the elbow bend the way the mapping assumes?

The mapping says "hand farther from the shoulder -> larger elbow joint angle".
That is a claim about the ROBOT's geometry, and it was derived by hand from
link offsets, which is exactly the sort of derivation that has already been
wrong twice in this repo (the finger rotation, twice, both times from summing
`xyz` while ignoring `rpy`).

So check it by forward kinematics on the URDF instead, with the rotations
applied: sweep the elbow joint, measure how far the wrist ends up from the
shoulder, and confirm that distance rises with the joint angle across the span
the mapping actually uses.

This is not circular. The mapping never consults the URDF -- it maps a measured
operator distance onto a joint range through two calibrated endpoints. Whether
that joint range points the way the mapping believes is a separate fact, and it
lives here.
"""
import math
import os
import xml.etree.ElementTree as ET

import numpy as np
import pytest

URDF = os.path.join(os.path.dirname(__file__), "..", "teleop", "assets", "g1",
                    "g1_body29_hand14.urdf")


def _rpy(r, p, y):
    cr, sr, cp, sp, cy, sy = (math.cos(r), math.sin(r), math.cos(p),
                              math.sin(p), math.cos(y), math.sin(y))
    return (np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]])
            @ np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]])
            @ np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]]))


def _axis_rot(axis, q):
    a = np.asarray(axis, float)
    n = np.linalg.norm(a)
    if n < 1e-12:
        return np.eye(3)
    a = a / n
    K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + math.sin(q) * K + (1 - math.cos(q)) * (K @ K)


def _joints():
    tree = ET.parse(URDF)
    out = {}
    for j in tree.getroot().findall("joint"):
        o = j.find("origin")
        xyz = [float(v) for v in ((o.get("xyz", "0 0 0") if o is not None
                                   else "0 0 0").split())]
        rpy = [float(v) for v in ((o.get("rpy", "0 0 0") if o is not None
                                   else "0 0 0").split())]
        ax = j.find("axis")
        out[j.get("name")] = {
            "child": j.find("child").get("link"),
            "xyz": np.asarray(xyz), "R": _rpy(*rpy),
            "axis": [float(v) for v in (ax.get("xyz").split() if ax is not None
                                        else "0 0 1".split())],
        }
    return out


def _wrist_from_shoulder(side, elbow_q):
    """Wrist position in the shoulder-pitch frame, all joints zero but the elbow.

    rpy is applied at every step. Skipping it is the mistake that produced a
    wrong finger rotation AND a test that agreed with it.
    """
    J = _joints()
    chain = [f"{side}_shoulder_roll_joint", f"{side}_shoulder_yaw_joint",
             f"{side}_elbow_joint", f"{side}_wrist_roll_joint"]
    p = np.zeros(3)
    T = np.eye(3)
    for name in chain:
        j = J[name]
        p = p + T @ j["xyz"]
        T = T @ j["R"]
        if name.endswith("elbow_joint"):
            T = T @ _axis_rot(j["axis"], elbow_q)
    return p


@pytest.mark.parametrize("side", ["left", "right"])
def test_reaching_out_really_does_mean_a_larger_elbow_angle(side):
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))
    import retarget_g1

    lo, hi = retarget_g1.G1Retargeter._ELBOW_SPAN
    qs = np.linspace(lo, hi, 40)
    d = np.array([np.linalg.norm(_wrist_from_shoulder(side, q)) for q in qs])
    # Monotonically increasing across the span the mapping uses -- otherwise
    # "reach out" would straighten the arm for part of the range and fold it
    # for the rest, and the operator would feel the sign invert mid-motion.
    assert np.all(np.diff(d) > -1e-6), (
        f"{side}: reach is not monotonic in elbow angle over "
        f"[{math.degrees(lo):.0f}, {math.degrees(hi):.0f}] deg: "
        f"{np.round(d, 4)}")
    assert d[-1] > d[0] + 0.05, (
        f"{side}: only {100*(d[-1]-d[0]):.1f} cm of reach across the whole span")


@pytest.mark.parametrize("side", ["left", "right"])
def test_the_span_stops_at_full_extension(side):
    """_ELBOW_SPAN's upper end should be straight, not past it.

    Past straight the arm folds the other way, so reach would start DECREASING
    again and the mapping would invert. The by-hand derivation put that point
    near +73 deg; this checks the geometry agrees.
    """
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))
    import retarget_g1

    _, hi = retarget_g1.G1Retargeter._ELBOW_SPAN
    qs = np.linspace(hi - 0.6, min(hi + 0.6, 2.0944), 60)
    d = np.array([np.linalg.norm(_wrist_from_shoulder(side, q)) for q in qs])
    peak = qs[int(np.argmax(d))]
    assert abs(peak - hi) < math.radians(12), (
        f"{side}: reach peaks at {math.degrees(peak):.1f} deg but _ELBOW_SPAN "
        f"ends at {math.degrees(hi):.1f} deg")


@pytest.mark.parametrize("side", ["left", "right"])
def test_the_operator_and_the_robot_agree_in_direction(side):
    """End to end: a longer operator reach must produce a longer robot reach."""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))
    import retarget_g1

    r = retarget_g1.G1Retargeter.__new__(retarget_g1.G1Retargeter)
    sh = retarget_g1.G1Retargeter._SHOULDER[side]
    near, far = retarget_g1.G1Retargeter._ELBOW_CAL[side]
    prev = None
    for frac in np.linspace(0.0, 1.0, 12):
        dist = near + frac * (far - near)
        w = (sh + np.array([0.0, 0.0, -dist])).tolist()
        q = r._elbow_q(side, {"wrist": w})
        reach = float(np.linalg.norm(_wrist_from_shoulder(side, q)))
        if prev is not None:
            assert reach >= prev - 1e-9, (
                f"{side}: operator reached further and the robot pulled in "
                f"({prev:.4f} -> {reach:.4f} m)")
        prev = reach
