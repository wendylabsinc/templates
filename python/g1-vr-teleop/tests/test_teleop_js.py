"""Directional tests for the head-frame math in web/teleop.js.

This file exists because two frame-convention bugs have now landed in
teleop.js, and the reason was structural: the JavaScript had no tests at all,
while test_direction.py covers only the Python constants.

Rather than duplicate the quaternion helpers here -- a mirror drifts, and a
mirror that drifts agrees with whichever version is wrong -- these tests
extract the REAL functions from teleop.js and run them under node.

The bug this was written for: headYawQuat used atan2(fx, -fz) where WebXR's
right-handed +Y-up, -Z-forward convention needs atan2(-fx, -fz). It returned
the negated yaw, rotating the wrist frame the wrong way by 2*theta. With the
operator turned 90 degrees and a hand in front of their face, the wrist came
out BEHIND the head -- worse than the full-head-rotation code it replaced.

It survived review because the checks run at the time were "does pitching the
head stop moving the target" (yaw is zero there, so the sign cannot show) and
"does turning the body still move the target" (it does -- in the wrong
direction). Both asked whether something responded. Neither asked whether it
was right.
"""
import json
import os
import shutil
import subprocess

import pytest

JS = os.path.join(os.path.dirname(__file__), "..", "teleop", "web", "teleop.js")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# The helpers under test, pulled from the real file by name.
_WANTED = ("quatInverse", "quatMul", "quatRotateVec", "sub", "headYawQuat", "levelAnchor", "panelPose", "hudLayerTransforms", "placeHudLayers")


def _extract():
    """Lift the named top-level functions out of teleop.js verbatim."""
    src = open(JS).read()
    out = []
    for name in _WANTED:
        marker = f"function {name}("
        i = src.index(marker)
        depth, j = 0, src.index("{", i)
        k = j
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
    script = _extract() + "\n" + body
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout)


AXIS_Q = """
const axisQ=(ax,ang)=>{const s=Math.sin(ang/2);return[ax[0]*s,ax[1]*s,ax[2]*s,Math.cos(ang/2)];};
const HEAD=[0,1.6,0];
// wrist in the head's yaw frame, exactly as handFeatures computes it
function wristInYawFrame(headQuat, wristWorld){
  const h=quatInverse(headYawQuat(headQuat));
  return quatRotateVec(h, sub(wristWorld, HEAD));
}
"""


@pytest.mark.parametrize("quest", [True, False])
def test_native_quads_keep_camera_and_directions_separate(quest):
    """Quest 3/OculusBrowser 152 renders native dimensions as half-extents.

    Reproduce the headset overlap through the actual layer creation call,
    rather than checking only the fallback drawing's intended rectangles.
    Other browsers retain the specification's full-width convention.
    """
    ua = "OculusBrowser/152.0.0 Quest 3" if quest else "Other WebXR Browser"
    rendered_factor = 2 if quest else 1
    got = run_js("""
      const HUD_SCALE=1.1, HUD_Y=-0.15, HUD_DIST=2.2;
      const STATUS_W=512, STATUS_H=288, HUD_W=512, HUD_H=264;
      Object.defineProperty(globalThis, 'navigator', {value:{userAgent:UA}, configurable:true});
      const XRRigidTransform=class {constructor(position,orientation){this.position=position;this.orientation=orientation;}};
      const hudLayers={binding:{createQuadLayer:init=>({...init})},proj:{}};
      const session={updateRenderState(){}};
      const anchor=[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1];
      placeHudLayers(session, {}, anchor);
      console.log(JSON.stringify(['cam','status','keys'].map(k=>{
        const l=hudLayers[k],p=l.transform.position;
        const w=l.width*FACTOR,h=l.height*FACTOR;
        return {w,h,left:p.x-w/2,right:p.x+w/2,bottom:p.y-h/2,top:p.y+h/2};
      })));
    """.replace("UA", json.dumps(ua)).replace("FACTOR", str(rendered_factor)))
    cam, status, keys = got
    assert status["right"] < cam["left"], "status overlaps the camera in the compositor"
    assert keys["right"] < cam["left"], "directions overlap the camera in the compositor"
    assert keys["top"] < status["bottom"], "directions cover the status text"
    assert cam["right"] < 0.48 * 1.1, "camera spills into the robot traces"
    assert (cam["w"], cam["h"]) == pytest.approx((0.88 * 1.1, 0.66 * 1.1))


def test_yaw_is_recovered_with_the_right_sign():
    got = run_js(AXIS_Q + """
    const out=[];
    for (const deg of [0,30,45,90,-60,-90]) {
      const t=deg*Math.PI/180;
      const f=quatRotateVec(axisQ([0,1,0],t),[0,0,-1]);
      out.push([deg, Math.atan2(-f[0],-f[2])*180/Math.PI]);
    }
    console.log(JSON.stringify(out));
    """)
    for want, got_deg in got:
        assert got_deg == pytest.approx(want, abs=1e-6), (
            f"head turned {want} deg recovered as {got_deg}")


@pytest.mark.parametrize("turn_deg", [0, 30, 90, -45, -90, 179])
def test_a_hand_in_front_of_the_face_stays_in_front(turn_deg):
    """THE test the first fix needed. Operator turns; hand stays in front of
    their face. In the yaw frame the wrist must stay straight ahead (-z),
    whichever way they are facing."""
    got = run_js(AXIS_Q + f"""
    const t={turn_deg}*Math.PI/180, hq=axisQ([0,1,0],t);
    const fwd=quatRotateVec(hq,[0,0,-1]);
    const wrist=[HEAD[0]+fwd[0]*0.5, HEAD[1], HEAD[2]+fwd[2]*0.5];
    console.log(JSON.stringify(wristInYawFrame(hq, wrist)));
    """)
    x, y, z = got
    assert z == pytest.approx(-0.5, abs=1e-6), (
        f"turned {turn_deg} deg, hand in front of the face -> z={z:+.3f}; "
        f"{'BEHIND the head' if z > 0 else 'wrong distance'}")
    assert abs(x) < 1e-6, f"hand drifted sideways by {x:+.3f} m"


@pytest.mark.parametrize("pitch_deg", [-45, -20, 0, 20, 45])
def test_head_pitch_does_not_move_a_fixed_hand(pitch_deg):
    """The original F1 finding: looking down must not move the arms."""
    got = run_js(AXIS_Q + f"""
    const level=[0,0,0,1];
    const pitched=axisQ([1,0,0],{pitch_deg}*Math.PI/180);
    const wrist=[0.2,1.2,-0.4];
    console.log(JSON.stringify([wristInYawFrame(level,wrist), wristInYawFrame(pitched,wrist)]));
    """)
    base, tilted = got
    moved = max(abs(a - b) for a, b in zip(base, tilted))
    assert moved < 1e-6, f"pitching {pitch_deg} deg moved the target {moved:.4f} m"


def test_turning_the_body_does_move_the_frame():
    """The complement: heading changes SHOULD move it, or we have removed
    something we needed."""
    got = run_js(AXIS_Q + """
    const wrist=[0.2,1.2,-0.4];
    console.log(JSON.stringify([
      wristInYawFrame([0,0,0,1],wrist),
      wristInYawFrame(axisQ([0,1,0],0.5),wrist)]));
    """)
    a, b = got
    assert max(abs(x - y) for x, y in zip(a, b)) > 0.1, "yaw was removed entirely"


def test_the_committed_formula_is_not_the_negated_one():
    """Guard the exact character that was wrong."""
    src = open(JS).read()
    assert "Math.atan2(-f[0], -f[2])" in src, "the yaw sign regressed"
    assert "Math.atan2(f[0], -f[2])" not in src


# ---- the room anchor the panels are fixed to (HUD_LOCK) ----------------------

MAT_FROM_POSE = """
// A head pose's own column-major matrix, as WebXR's transform.matrix is.
function poseMatrix(q, p){const [x,y,z,w]=q;
  return [1-2*(y*y+z*z), 2*(x*y+z*w), 2*(x*z-y*w), 0,
          2*(x*y-z*w), 1-2*(x*x+z*z), 2*(y*z+x*w), 0,
          2*(x*z+y*w), 2*(y*z-x*w), 1-2*(x*x+y*y), 0, p[0], p[1], p[2], 1];}
"""


def test_a_level_head_is_its_own_anchor():
    """Looking straight ahead, the room anchor IS the head pose, so the
    panels appear exactly where the head-locked layout put them."""
    out = run_js(AXIS_Q + MAT_FROM_POSE + """
    const res=[];
    for (const deg of [0, 35, -120, 179]) {
      const q=axisQ([0,1,0], deg*Math.PI/180), p=[0.3,1.62,-0.4];
      const a=Array.from(levelAnchor({pos:p, quat:q})), m=poseMatrix(q,p);
      res.push(Math.max(...a.map((v,i)=>Math.abs(v-m[i]))));
    }
    console.log(JSON.stringify(res));""")
    assert max(out) < 1e-6, out


def test_the_anchor_keeps_heading_and_drops_pitch_and_roll():
    """Entering VR looking down at the robot must not tilt the panels."""
    out = run_js(AXIS_Q + """
    const yaw=axisQ([0,1,0], 0.6), pitch=axisQ([1,0,0], -0.7), roll=axisQ([0,0,1], 0.3);
    const a=levelAnchor({pos:[0,1.5,0], quat:quatMul(quatMul(yaw,pitch),roll)});
    console.log(JSON.stringify({up:[a[4],a[5],a[6]], fwd:[-a[8],-a[9],-a[10]]}));""")
    # Float32Array, as WebGL takes it: ~1e-7 is the precision there is.
    assert out["up"] == pytest.approx([0, 1, 0], abs=1e-6)
    import math
    assert out["fwd"] == pytest.approx([-math.sin(0.6), 0, -math.cos(0.6)], abs=1e-6)



def test_a_quad_layer_sits_exactly_where_the_in_page_panel_did():
    """panelMvp places the panel's centre at anchor * (x*k, y*k, -dist) and
    faces it along the anchor's heading; the layer must match both, or the
    panels would jump when the compositor takes them over."""
    out = run_js(AXIS_Q + """
    const res=[];
    for (const deg of [0, 40, -150]) {
      const a=levelAnchor({pos:[0.2,1.55,-0.3], quat:axisQ([0,1,0], deg*Math.PI/180)});
      const x=-0.82, y=-0.03, d=2.2, k=1.35;
      const P=panelPose(a, x, y, d, k);
      const want=[a[0]*x*k+a[4]*y*k-a[8]*d+a[12], a[1]*x*k+a[5]*y*k-a[9]*d+a[13], a[2]*x*k+a[6]*y*k-a[10]*d+a[14]];
      // the layer's +Z (its facing) rotated by its orientation must equal the anchor's +Z column
      const f=quatRotateVec([P.orientation.x,P.orientation.y,P.orientation.z,P.orientation.w],[0,0,1]);
      res.push(Math.max(Math.abs(P.position.x-want[0]),Math.abs(P.position.y-want[1]),Math.abs(P.position.z-want[2]),
                        Math.abs(f[0]-a[8]),Math.abs(f[1]-a[9]),Math.abs(f[2]-a[10])));
    }
    console.log(JSON.stringify(res));""")
    assert max(out) < 1e-5, out
