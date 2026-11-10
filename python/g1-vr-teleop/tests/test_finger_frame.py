"""Forward kinematics on the Dex3 hand, done correctly.

This file exists because an earlier version of it asserted the fingers ran
along +X and PASSED -- because it computed the tip positions by summing joint
`xyz` offsets while ignoring their `rpy`. A test that repeats the measurement
mistake it is meant to catch is worse than no test: it was cited as evidence
for a rotation that scored 6 % of usable finger range on the robot.

Two things it now pins:

1. FK includes the rotations. With them, the standalone hand URDF that
   dex_retargeting actually loads puts the fingers along -Y, not +X.
2. That standalone URDF is a DIFFERENT frame from the hand as mounted in the
   whole-body model. dex_retargeting loads the standalone one.

The rotation matrices themselves are not asserted here. They were chosen
empirically against a recorded session, not derived
from this geometry, and a geometric assertion would only re-create the trap
above.
"""
import os
import xml.etree.ElementTree as ET

import numpy as np
import pytest

ASSETS = os.path.join(os.path.dirname(__file__), "..", "teleop", "assets", "unitree_hand")


def _rpy(rpy):
    cr, cp, cy = np.cos(rpy)
    sr, sp, sy = np.sin(rpy)
    return (np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]])
            @ np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]])
            @ np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]]))


def tips(side, with_rpy=True):
    """Fingertip positions in the standalone hand's base_link frame."""
    r = ET.parse(os.path.join(ASSETS, f"unitree_dex3_{side}.urdf")).getroot()
    J = {j.get("name"): j for j in r.findall("joint")}

    def pos(link):
        cur, chain = link, []
        while True:
            up = [n for n, j in J.items() if j.find("child").get("link") == cur]
            if not up:
                break
            chain.append(up[0])
            cur = J[up[0]].find("parent").get("link")
        p, R = np.zeros(3), np.eye(3)
        for n in reversed(chain):
            o = J[n].find("origin")
            xyz = np.array([float(v) for v in o.get("xyz", "0 0 0").split()]) if o is not None else np.zeros(3)
            rot = np.array([float(v) for v in o.get("rpy", "0 0 0").split()]) if o is not None else np.zeros(3)
            p = p + (R @ xyz if with_rpy else xyz)
            if with_rpy:
                R = R @ _rpy(rot)
        return p
    return {k: pos(k) for k in ("thumb_tip", "index_tip", "middle_tip")}


@pytest.mark.parametrize("side", ["left", "right"])
def test_the_fingers_run_along_minus_y(side):
    t = tips(side)
    axis = (t["index_tip"] + t["middle_tip"]) / 2
    axis = axis / np.linalg.norm(axis)
    assert axis[1] < -0.99, (
        f"{side} finger axis is {axis.round(3)}, expected roughly -Y. If this "
        "reads +X, the FK is ignoring joint rpy again.")


@pytest.mark.parametrize("side", ["left", "right"])
def test_ignoring_rpy_gives_the_wrong_answer(side):
    """Characterise the mistake, so nobody re-introduces the shortcut."""
    wrong = tips(side, with_rpy=False)
    axis = (wrong["index_tip"] + wrong["middle_tip"]) / 2
    axis = axis / np.linalg.norm(axis)
    assert axis[0] > 0.99, "the shortcut no longer reproduces the old error"
    right = tips(side)
    assert not np.allclose(wrong["index_tip"], right["index_tip"], atol=1e-3)


@pytest.mark.parametrize("side", ["left", "right"])
def test_index_and_middle_are_separated_on_z(side):
    t = tips(side)
    d = t["index_tip"] - t["middle_tip"]
    assert abs(d[2]) > 0.99 * np.linalg.norm(d), (
        f"{side} index-middle separation is {d.round(4)}, expected along Z")


def test_the_standalone_urdf_is_not_the_whole_body_one():
    """dex_retargeting loads the standalone hand. Measuring the whole-body
    mount instead is how the wrong frame got used as evidence."""
    whole = os.path.join(os.path.dirname(__file__), "..", "teleop", "assets", "g1",
                         "g1_body29_hand14.urdf")
    assert os.path.exists(whole)
    body = open(whole).read()
    assert "left_hand_index_0_joint" in body, "whole-body model lacks the hand"
    stand = open(os.path.join(ASSETS, "unitree_dex3_left.urdf")).read()
    assert "base_link" in stand, "the standalone hand has its own root frame"
    # The whole-body model roots the hand at the wrist; the standalone one roots
    # it at base_link. Same hand, different frames -- which is the whole point.
    assert "base_link" not in body.split("left_hand_index_0_joint")[0][-4000:], \
        "the two models look like they share a root frame"
    assert body != stand
