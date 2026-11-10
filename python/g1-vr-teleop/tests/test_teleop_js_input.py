"""The headset chooses its input source: hands, controllers, or whatever it holds.

Same approach as test_teleop_js.py: the real functions are lifted out of
web/teleop.js and run under node, so there is no drifting copy to disagree.
"""
import json
import os
import shutil
import subprocess

import pytest

JS = os.path.join(os.path.dirname(__file__), "..", "teleop", "web", "teleop.js")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

_WANTED = ("quatInverse", "quatMul", "quatRotateVec", "sub", "headYawQuat",
           "sourceKind", "sourceAllowed", "controllerFeatures")


def _extract():
    src = open(JS).read()
    out = []
    for name in _WANTED:
        marker = f"function {name}("
        i = src.index(marker)
        depth, k = 0, src.index("{", i)
        while True:
            if src[k] == "{":
                depth += 1
            elif src[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        out.append(src[i:k + 1])
    return "\n".join(out)


def run_js(body):
    r = subprocess.run([NODE, "-e", _extract() + "\n" + body], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


HAND = "{hand: new Map(), handedness: 'left'}"
CTRL = "{gripSpace: {}, gamepad: {buttons: [], axes: []}, handedness: 'right'}"
GAZE = "{handedness: 'none'}"
CTRL_NO_PAD = "{gripSpace: {}, handedness: 'right'}"


def test_source_kinds():
    out = run_js(f"console.log(JSON.stringify([sourceKind({HAND}), sourceKind({CTRL}), "
                 f"sourceKind({GAZE}), sourceKind({CTRL_NO_PAD})]))")
    assert out == ["hand", "controller", None, None]


@pytest.mark.parametrize("mode,expect", [
    ("auto", [True, True, False]),
    ("hands", [True, False, False]),
    ("controllers", [False, True, False]),
])
def test_each_mode_admits_exactly_what_it_says(mode, expect):
    out = run_js(f"console.log(JSON.stringify(['hand','controller',null].map(k => sourceAllowed('{mode}', k))))")
    assert out == expect


# String concatenation, not f-strings: `wendy init` renders .py files as Go
# templates, so this file must never contain a double opening brace.
EMPTY = "{}"


def _frame_js(pos, quat):
    return ("{getPose: () => ({transform: {position: {x:%s, y:%s, z:%s}, "
            "orientation: {x:%s, y:%s, z:%s, w:%s}}})}" % (*pos, *quat))


def test_controller_features_have_the_hand_shape_and_the_raw_grip_orientation():
    src = ("{gripSpace: {}, handedness: 'right', gamepad: {buttons: [{value: 0.7}, {value: 0.2}, {}, {}, "
           "{pressed: true}, {pressed: false}], axes: [0, 0, 0.5, -0.25]}}")
    head = "{pos: [0, 1.6, 0], quat: [0, 0, 0, 1]}"
    out = run_js("console.log(JSON.stringify(controllerFeatures("
                 + _frame_js([0.3, 1.2, -0.5], [0.1, 0.2, 0.3, 0.927])
                 + ", " + EMPTY + ", " + src + ", " + head + ")))")
    assert out["hand"] == "right" and out["source"] == "controller"
    assert out["landmarks"] is None and out["fabricated"] == 0
    assert out["wrist"] == pytest.approx([0.3, -0.4, -0.5])            # head at 1.6 m, no yaw
    assert out["quat"] == pytest.approx([0.1, 0.2, 0.3, 0.927], abs=1e-4)  # RAW: the bridge aligns it
    assert out["trigger"] == pytest.approx(0.7) and out["squeeze"] == pytest.approx(0.2)
    assert out["thumbstick"] == pytest.approx([0.5, -0.25])
    # `stick` joined them when both thumbstick clicks became the arm control.
    assert out["buttons"] == {"primary": True, "secondary": False, "stick": False}


def test_a_controller_in_front_of_a_turned_operator_stays_in_front():
    """The same heading-frame rule the wrist uses: turn 90 deg, hold the
    controller in front of your face, and it must come out in front."""
    import math
    s, c = math.sin(math.radians(45)), math.cos(math.radians(45))   # yaw 90 deg about Y
    head = "{pos: [0, 1.6, 0], quat: [0, %s, 0, %s]}" % (s, c)
    # facing +X after a +90 yaw (WebXR: -Z forward, so +90 about Y turns forward onto -X ... see headYawQuat)
    out_fwd = run_js(f"const h={head}; const f=headYawQuat(h.quat); "
                     f"const fwd=quatRotateVec(h.quat,[0,0,-1]); "
                     "console.log(JSON.stringify(controllerFeatures("
                     + _frame_js(['0.5*fwd[0]', '1.6+0.5*fwd[1]', '0.5*fwd[2]'], [0, 0, 0, 1])
                     + ", " + EMPTY + ", " + CTRL + ", h).wrist))")
    assert out_fwd == pytest.approx([0, 0, -0.5], abs=1e-3)


def test_a_source_that_yields_no_pose_is_skipped():
    src = "{gripSpace: {}, gamepad: {buttons: [], axes: []}, handedness: 'right'}"
    out = run_js("console.log(JSON.stringify(controllerFeatures({getPose: () => null}, "
                 + EMPTY + ", " + src + ", {pos:[0,0,0], quat:[0,0,0,1]})))")
    assert out is None


def test_hand_entries_now_say_they_are_hands():
    src = open(JS).read()
    hf = src.split("function handFeatures(")[1].split("\nfunction ")[0]
    assert 'source: "hand"' in hf
