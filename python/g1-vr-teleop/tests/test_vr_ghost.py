"""The miniature robot in the headset draws the robot that exists.

Away from the robot the operator cannot see where the hands are: the head
camera looks at the table and the arms enter its cone only when reaching.
web/robot_ghost.js draws the G1's upper body from the bridge's "robot"
messages -- measured pose solid, commanded pose as a ghost.

What can go wrong, and what is checked here:

- The JavaScript forward kinematics disagrees with the Python it was derived
  beside. Two frame-convention bugs have landed in teleop.js before; the JS
  had no tests. So the real module runs under node against retarget_g1's
  forearm_orientation and an independent numpy chain walk.
- A stale measurement is drawn as the robot's live pose. That is the one way
  this display could make the operator trust a robot that has stopped
  answering. The stale rule is tested on its own.
- The show/hide gesture fires during the arming press. Both sticks arm; the
  left stick alone toggles; the right stick during the hold must cancel.
"""
import json
import math
import os
import pathlib
import shutil
import subprocess
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))
import retarget_g1 as rg                                         # noqa: E402

JS = ROOT / "teleop" / "web" / "robot_ghost.js"
TELEOP = (ROOT / "teleop" / "web" / "teleop.js").read_text()
CHAIN = json.loads((ROOT / "teleop" / "web" / "g1_chain.json").read_text())
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


def run_js(body):
    """Run `body` with the real module imported as `g` and the chain as `chain`."""
    # Concatenated, not an f-string: `wendy init` renders .py files as Go
    # templates, so this file must never contain a double opening brace.
    chain = json.dumps(str(ROOT / 'teleop' / 'web' / 'g1_chain.json'))
    script = ("import('" + JS.as_uri() + "').then(async (g) => {\n"
              "  const chain = JSON.parse(require('fs').readFileSync(" + chain + "));\n"
              + body + "\n"
              "}).catch((e) => { console.error(e); process.exit(1); });\n")
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout)


def _T(xyz, rpy, axis=None, q=0.0):
    """One URDF joint as a 4x4, written independently of the JS."""
    r, p, y = rpy
    Rz = np.array([[math.cos(y), -math.sin(y), 0], [math.sin(y), math.cos(y), 0], [0, 0, 1]])
    Ry = np.array([[math.cos(p), 0, math.sin(p)], [0, 1, 0], [-math.sin(p), 0, math.cos(p)]])
    Rx = np.array([[1, 0, 0], [0, math.cos(r), -math.sin(r)], [0, math.sin(r), math.cos(r)]])
    T = np.eye(4)
    T[:3, :3] = Rz @ Ry @ Rx
    T[:3, 3] = xyz
    if axis is not None and q:
        k = np.asarray(axis, float)
        K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
        R = np.eye(3) + math.sin(q) * K + (1 - math.cos(q)) * K @ K
        J = np.eye(4)
        J[:3, :3] = R
        T = T @ J
    return T


def py_fk(q):
    links = {CHAIN["root_link"]: np.eye(4)}
    frames = {}
    for j in CHAIN["joints"]:
        frame = links[j["parent"]] @ _T(j["xyz"], j["rpy"])
        link = frame if j["type"] == "fixed" else links[j["parent"]] @ _T(
            j["xyz"], j["rpy"], j["axis"], q.get(j["name"], 0.0))
        links[j["child"]] = link
        frames[j["name"]] = (frame, link)
    return frames


def _col_major(m16):
    return np.array(m16, float).reshape(4, 4).T


# ---- forward kinematics ----------------------------------------------------

@needs_node
@pytest.mark.parametrize("seed", range(4))
def test_every_joint_frame_matches_an_independent_chain_walk(seed):
    """All 35 joints, random angles inside their limits, waist and fingers
    included: the JS and a numpy walk written separately agree to a micron."""
    rng = np.random.default_rng(seed)
    q = {j["name"]: float(rng.uniform(*j["limit"])) for j in CHAIN["joints"] if j["type"] != "fixed"}
    got = run_js("const fk = g.chainFK(chain, " + json.dumps(q) + ");\n"
                 "const out = {};\n"
                 "for (const [n, v] of Object.entries(fk)) "
                 "out[n] = {frame: Array.from(v.frame), link: Array.from(v.link)};\n"
                 "console.log(JSON.stringify(out));\n")
    want = py_fk(q)
    assert set(got) == set(want)
    for name, (frame, link) in want.items():
        assert _col_major(got[name]["frame"]) == pytest.approx(frame, abs=1e-6), name
        assert _col_major(got[name]["link"]) == pytest.approx(link, abs=1e-6), name


@needs_node
@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("pitch,roll,elbow,yaw", [
    (0.0, 0.0, 0.0, 0.0), (0.7, -0.4, 1.2, 0.3), (-1.1, 0.9, 0.4, -0.6)])
def test_the_forearm_frame_is_the_one_the_wrist_math_uses(side, pitch, roll, elbow, yaw):
    """retarget_g1.forearm_orientation is the frame the three wrist joints act
    in, and it is what put the hand 108 deg off when it was ignored. The
    miniature's wrist roll joint frame must be that same rotation, or the
    robot on screen and the robot being commanded are two different robots."""
    q = {f"{side}_shoulder_pitch_joint": pitch, f"{side}_shoulder_roll_joint": roll,
         f"{side}_shoulder_yaw_joint": yaw, f"{side}_elbow_joint": elbow}
    got = run_js(f"""
      const fk = g.chainFK(chain, {json.dumps(q)});
      console.log(JSON.stringify(Array.from(fk["{side}_wrist_roll_joint"].frame)));
    """)
    R = _col_major(got)[:3, :3]
    assert R == pytest.approx(rg.forearm_orientation(side, pitch, roll, elbow, yaw), abs=1e-6)


@needs_node
def test_the_robot_to_xr_rotation_is_the_inverse_of_the_bridges():
    """The miniature is placed by the inverse of _R_XR_TO_ROBOT. Held equal
    here so the two cannot drift apart; a mismatch would show the robot's
    right arm on the operator's left."""
    got = run_js("console.log(JSON.stringify(Array.from(g.ROBOT_TO_XR)));")
    R = _col_major(got)[:3, :3]
    assert R @ rg._R_XR_TO_ROBOT == pytest.approx(np.eye(3), abs=1e-12)


@needs_node
def test_a_forward_reach_draws_forward_and_away_from_the_operator():
    """Sign check that can read negative: raise the left arm forward (shoulder
    pitch negative on the G1) and the wrist must move to robot +X, which in the
    miniature is -Z (away from the operator), on the operator's LEFT (-X)."""
    got = run_js("""
      const home = g.m4origin(g.chainFK(chain, {}).left_wrist_roll_joint.frame);
      const fwd = g.m4origin(g.chainFK(chain, {left_shoulder_pitch_joint: -1.2}).left_wrist_roll_joint.frame);
      const xr = (p) => g.m4point(g.ROBOT_TO_XR, p);
      console.log(JSON.stringify({home, fwd, xrHome: xr(home), xrFwd: xr(fwd)}));
    """)
    assert got["fwd"][0] > got["home"][0] + 0.1, "robot +X is forward"
    assert got["xrFwd"][2] < got["xrHome"][2] - 0.1, "forward is -Z in the headset"
    assert got["xrFwd"][0] < 0, "the left arm is on the operator's left"


# ---- the bones -----------------------------------------------------------------

@needs_node
def test_the_fingertip_reaches_the_mesh_not_the_last_joint():
    """The joint tree ends at index_1; the finger does not. At the zero pose
    the index tip must be the mesh extent (52 mm) beyond the index_1 origin."""
    got = run_js("""
      const fk = g.chainFK(chain, {});
      const b = g.bones(chain, fk);
      const j1 = g.m4origin(fk.left_hand_index_1_joint.frame);
      const tips = b.filter((s) => Math.hypot(...s.a.map((v, i) => v - j1[i])) < 1e-9 && s.r < 0.01);
      console.log(JSON.stringify({n: b.length, tips: tips.map((s) => Math.hypot(...s.b.map((v, i) => v - s.a[i])))}));
    """)
    ext = CHAIN["links"]["left_hand_index_1_link"]["extent"]
    assert got["tips"] and max(got["tips"]) == pytest.approx(ext[1][0], abs=1e-4)


@needs_node
def test_tip_of_keeps_the_sign_of_the_dominant_axis():
    """The left thumb's last link extends to NEGATIVE y, the right's to
    positive. A tip that took the absolute value would point both thumbs the
    same way."""
    got = run_js("""
      console.log(JSON.stringify([g.tipOf(chain.links.left_hand_thumb_2_link.extent),
                                  g.tipOf(chain.links.right_hand_thumb_2_link.extent),
                                  g.tipOf(chain.links.left_hand_index_1_link.extent)]));
    """)
    assert got[0][1] < -0.04 and got[1][1] > 0.04
    assert got[2][0] > 0.04 and got[2][1] == 0 and got[2][2] == 0


@needs_node
def test_every_bone_is_finite_and_the_arms_hang_from_the_shoulders():
    got = run_js("""
      const fk = g.chainFK(chain, {});
      const b = g.bones(chain, fk);
      const sp = g.m4origin(fk.left_shoulder_pitch_joint.frame);
      const hasShoulder = b.some((s) => s.a.every((v, i) => Math.abs(v - sp[i]) < 1e-9));
      console.log(JSON.stringify({finite: b.every((s) => [...s.a, ...s.b, s.r].every(Number.isFinite)),
                                  hasShoulder, n: b.length}));
    """)
    assert got["finite"] and got["hasShoulder"]
    assert got["n"] >= 30


# ---- what is drawn: the stale rule ----------------------------------------------

@needs_node
def test_a_message_that_stopped_arriving_hides_everything():
    """The bridge went quiet: nothing on screen may claim to be current."""
    got = run_js("""
      const m = {type: "robot", q: new Array(14).fill(0), q_age_s: 0.01, cmd: {arm: new Array(14).fill(0)}};
      console.log(JSON.stringify([g.ghostVisibility(m, 0, 100), g.ghostVisibility(m, 0, 2000)]));
    """)
    assert got[0] == {"solid": True, "ghost": True}
    assert got[1] == {"solid": False, "ghost": False}


@needs_node
def test_an_old_measurement_hides_the_solid_robot_but_keeps_the_ghost():
    """The bridge is alive and the onboard is not answering. The measured arm
    is from before the silence and must not be drawn as where the robot is;
    the command is still what is being asked, so the ghost stays."""
    got = run_js("""
      const m = {type: "robot", q: new Array(14).fill(0), q_age_s: 3.0, cmd: {arm: new Array(14).fill(0)}};
      console.log(JSON.stringify(g.ghostVisibility(m, 0, 100)));
    """)
    assert got == {"solid": False, "ghost": True}


@needs_node
def test_no_measurement_and_no_command_draws_nothing():
    got = run_js("""
      const m = {type: "robot", q: null, q_age_s: null, cmd: {arm: null, waist: null, hands: null}};
      console.log(JSON.stringify([g.ghostVisibility(m, 0, 10), g.ghostVisibility(null, 0, 10),
                                  g.ghostVisibility({type: "hello"}, 0, 10)]));
    """)
    assert all(v == {"solid": False, "ghost": False} for v in got)


@needs_node
def test_a_partial_command_falls_back_to_the_measured_joint():
    """Forearm-only modes command four joints a side. The ghost's shoulders
    must be the MEASURED shoulders, not zero -- zero would draw a ghost arm
    sticking out where nothing is being asked to go."""
    got = run_js("""
      const measured = g.jointMap(chain, new Array(14).fill(0.5), 0.2, null, null);
      const cmd = new Array(14).fill(null); cmd[3] = 1.5;      // left elbow only
      const ghostQ = g.jointMap(chain, cmd, null, null, measured);
      console.log(JSON.stringify(ghostQ));
    """)
    assert got["left_elbow_joint"] == 1.5
    assert got["left_shoulder_pitch_joint"] == 0.5
    assert got["waist_yaw_joint"] == 0.2


# ---- the gesture ----------------------------------------------------------------

@needs_node
def test_holding_the_left_stick_alone_toggles_once():
    got = run_js("""
      let s = {shown: true}; const out = [];
      for (const t of [0, 200, 400, 650, 900, 1500]) { s = g.ghostToggle(s, true, false, t); out.push(s.shown); }
      s = g.ghostToggle(s, false, false, 1600); out.push(s.shown);
      console.log(JSON.stringify(out));
    """)
    assert got == [True, True, True, False, False, False, False], "toggle once at the hold, not again while held"


@needs_node
def test_a_tap_does_nothing_and_the_hold_can_be_repeated():
    got = run_js("""
      let s = {shown: true};
      s = g.ghostToggle(s, true, false, 0); s = g.ghostToggle(s, true, false, 300); s = g.ghostToggle(s, false, false, 310);
      const afterTap = s.shown;
      s = g.ghostToggle(s, true, false, 1000); s = g.ghostToggle(s, true, false, 1700); s = g.ghostToggle(s, false, false, 1710);
      s = g.ghostToggle(s, true, false, 2000); s = g.ghostToggle(s, true, false, 2700);
      console.log(JSON.stringify([afterTap, s.shown]));
    """)
    assert got == [True, True], "a tap is not a hold; two holds cancel out"


@needs_node
def test_the_right_stick_during_the_hold_cancels_it():
    """Arming is both sticks. Pressed a hair apart, the left one starts this
    hold; the right one arriving must end it, or arming also hides the robot."""
    got = run_js("""
      let s = {shown: true};
      s = g.ghostToggle(s, true, false, 0);
      s = g.ghostToggle(s, true, true, 50);
      s = g.ghostToggle(s, true, false, 700);
      s = g.ghostToggle(s, true, false, 2000);
      console.log(JSON.stringify(s.shown));
    """)
    assert got is True


@needs_node
def test_url_knobs_clamp_and_never_revert():
    got = run_js("""
      console.log(JSON.stringify([g.ghostParams(""), g.ghostParams("?ghost=0"),
                                  g.ghostParams("?ghostscale=99&ghosty=-9&ghostyaw=abc")]));
    """)
    assert got[0]["enabled"] is True and got[1]["enabled"] is False
    assert got[2]["scale"] == 2 and got[2]["y"] == -1.5 and got[2]["yawDeg"] == got[0]["yawDeg"]


# ---- wiring in the page ----------------------------------------------------------

def test_the_page_imports_the_module_and_draws_it_every_frame():
    assert 'import * as ghost from "./robot_ghost.js";' in TELEOP
    assert "ghost.drawGhost(glRef, ghostGL, ghostChain, robotMsg, robotRecvMs" in TELEOP
    assert "drawOverlayView(glRef, view, vp, hudMatrix);" in TELEOP.split("ghost.drawGhost(glRef")[0], \
        "the miniature is drawn after the panels"


def test_the_page_takes_the_robot_message_off_the_teleop_socket():
    assert "ws.onmessage" in TELEOP
    assert 'm.type === "robot"' in TELEOP
    assert "robotRecvMs = performance.now()" in TELEOP, "the stale rule needs the arrival time"


def test_the_gesture_is_in_the_legend_and_says_which_way_it_goes():
    hud = (ROOT / "teleop" / "web" / "hud.js").read_text()
    legend = hud.split("const rows = [")[1].split("];")[0]
    assert "left stick (hold)" in legend
    assert 'state.ghostShown === false ? "Show robot" : "Hide robot"' in legend
    assert "setHud({ ghostShown: ghostState.shown })" in TELEOP


def test_it_can_be_left_off_and_a_failure_costs_nothing():
    assert "ghost.ghostParams(location.search)" in TELEOP
    assert "if (GHOST.enabled)" in TELEOP
    setup = TELEOP.split("ghost.setupGhostGL(gl)")[0][-200:]
    assert "try {" in setup, "a shader that fails to compile must not end the session"
    assert ".catch((e) => log(\"robot miniature off: no chain" in TELEOP


def test_the_chain_is_served_from_the_web_directory():
    """The page fetches /g1_chain.json; the bridge serves web/ statically and
    the Dockerfile copies web/, so the file ships with the page."""
    assert (ROOT / "teleop" / "web" / "g1_chain.json").exists()
    assert "COPY web ./web" in (ROOT / "teleop" / "Dockerfile").read_text()
    assert 'loadChain(url = "/g1_chain.json")' in JS.read_text()


@needs_node
def test_the_ghost_draws_the_command_not_a_second_torso():
    """The command is arms, hands and waist. With a measured robot on screen
    the ghost adds nothing by redrawing the torso and head over it."""
    got = run_js("""
      const fk = g.chainFK(chain, {});
      console.log(JSON.stringify([g.bones(chain, fk).length, g.bones(chain, fk, true).length]));
    """)
    assert got[0] - got[1] == 2, "torso capsule and head ball"
    assert "const armsOnly = !!solidFK;" in JS.read_text(), "the ghost is arms-only exactly when a measured robot is drawn"


# ---- the real meshes -------------------------------------------------------------

MESH_JSON = ROOT / "teleop" / "web" / "g1_meshes.json"
MESH_BIN = ROOT / "teleop" / "web" / "g1_meshes.bin"

MESH_BODY = f"""
  const fs = require('fs');
  const man = JSON.parse(fs.readFileSync({json.dumps(str(MESH_JSON))}));
  const buf = fs.readFileSync({json.dumps(str(MESH_BIN))});
  const meshes = g.parseMeshes(man, buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
"""


@needs_node
def test_the_page_parses_the_packed_meshes_exactly_as_the_manifest_says():
    got = run_js(MESH_BODY + """
      const out = {};
      for (const [n, m] of Object.entries(meshes)) {
        let mx = -1; for (const i of m.idx) if (i > mx) mx = i;
        out[n] = {verts: m.pos.length / 3, tris: m.idx.length / 3, maxIdx: mx,
                  bounds: [Math.min(...m.pos.filter((_, i) => i % 3 === 0)), Math.max(...m.pos.filter((_, i) => i % 3 === 0))]};
      }
      console.log(JSON.stringify(out));
    """)
    man = json.loads(MESH_JSON.read_text())
    for name, e in man["links"].items():
        assert got[name]["verts"] == e["verts"] and got[name]["tris"] == e["tris"], name
        assert got[name]["maxIdx"] < e["verts"], name
        assert got[name]["bounds"][0] == pytest.approx(e["bounds"][0][0], abs=1e-4)


@needs_node
def test_a_binary_of_the_wrong_length_is_refused_not_misread():
    got = run_js("""
      const man = JSON.parse(require('fs').readFileSync(%s));
      let err = null;
      try { g.parseMeshes(man, new ArrayBuffer(10)); } catch (e) { err = e.message; }
      console.log(JSON.stringify(err));
    """ % json.dumps(str(MESH_JSON)))
    assert "bytes" in got


@needs_node
def test_every_mesh_has_a_pose_at_every_joint_state():
    """linkPoses places the child of every joint; the pelvis is the root.
    A mesh without a pose is a link silently not drawn."""
    got = run_js(MESH_BODY + """
      const poses = Object.assign(g.rootPose(chain), g.linkPoses(chain, g.chainFK(chain, {})));
      const missing = Object.keys(meshes).filter((n) => !poses[n]);
      console.log(JSON.stringify(missing));
    """)
    assert got == []


@needs_node
def test_the_ghost_is_arms_and_hands_only():
    got = run_js("""
      console.log(JSON.stringify(["left_shoulder_roll_link", "right_hand_index_1_link", "torso_link",
                                  "head_link", "pelvis", "waist_yaw_link"].map(g.isArmLink)));
    """)
    assert got == [True, True, False, False, False, False]


def test_meshes_are_optional_and_capsules_are_the_fallback():
    src = JS.read_text()
    assert "gpu = null" in src
    assert 'getExtension("OES_element_index_uint")' in src, "32-bit indices, or fall back"
    assert "solidFK && !gpu ? bones(" in src
    assert "gl.depthMask(false);" in src, "a translucent ghost must not hide the real arm"


def test_the_page_loads_the_meshes_and_hands_them_to_the_drawing():
    assert "ghost.loadMeshes()" in TELEOP
    assert "ghostMeshes = ghost.uploadMeshes(gl, m)" in TELEOP
    assert "ghostMeshes);" in TELEOP.split("ghost.drawGhost(")[1][:300]
    assert 'log("robot miniature: capsules only' in TELEOP


def test_lighting_is_two_sided():
    """Decimation flips a few windings; one-sided lighting drew them as dark
    speckles all over the torso."""
    assert "abs(dot(normalize(vN), normalize(light)))" in JS.read_text()


# ---- two views, from above ---------------------------------------------------------
#
# In the headset, from straight behind and a little turned, the torso hid the
# right hand: "i dont see the right hand". A single view from behind cannot
# show a hand that is in front of the body. So: look down from above and
# behind, which puts a forward reach below the shoulders, and add a second
# copy from the side.

@needs_node
def test_looking_down_from_behind_puts_a_forward_reach_below_the_shoulders():
    """The pitch knob, checked where it matters. Level, a hand in front of
    the robot (robot +X) sits in the shoulder line and the torso hides it.
    Seen from above -- the robot tipped toward the viewer -- farther is
    higher on screen, so the same reach separates from the shoulders and
    shows."""
    got = run_js("""
      const fk = g.chainFK(chain, {right_shoulder_pitch_joint: -1.4});   // right arm straight forward
      const sh = g.m4origin(fk.right_shoulder_pitch_joint.frame), wr = g.m4origin(fk.right_wrist_roll_joint.frame);
      const p = g.ghostParams("");
      const flat = g.placement(g.m4identity(), Object.assign({}, p, {pitchDeg: 0}), 1.4);
      const down = g.placement(g.m4identity(), p, 1.4);
      const dy = (m) => g.m4point(m, wr)[1] - g.m4point(m, sh)[1];
      console.log(JSON.stringify({flat: dy(flat), down: dy(down), pitch: p.pitchDeg}));
    """)
    assert got["pitch"] > 0, "the default looks down"
    assert abs(got["flat"]) < 0.04, "level: a forward reach is at shoulder height, hidden"
    assert got["down"] > 0.05, "tilted: the wrist leaves the shoulder line (farther is higher from above)"


@needs_node
def test_two_views_by_default_one_to_four_on_request():
    """Two by default: every copy is ~150k triangles per eye at 90 Hz and the
    Quest under four is unmeasured. Four is one knob away."""
    got = run_js("""
      const a = g.ghostViews(g.ghostParams(""));
      const b = g.ghostViews(g.ghostParams("?ghostviews=1"));
      const c = g.ghostViews(g.ghostParams("?ghostviews=7"));
      const d = g.ghostViews(g.ghostParams("?ghostviews=4"));
      console.log(JSON.stringify({a: a.length, b: b.length, c: c.length, d: d.length,
        angles: d.map((v) => [v.yawDeg, v.pitchDeg]), sameScale: d.every((v) => v.scale === d[0].scale)}));
    """)
    assert got["a"] == 2 and got["b"] == 1 and got["c"] == 4 and got["d"] == 4
    assert len({tuple(a) for a in got["angles"]}) == 4, "four views from four different angles"
    assert got["sameScale"], "every copy is the same robot at the same size"


@needs_node
def test_the_views_do_not_overlap_each_other_or_the_camera_panel():
    """The camera panel spans x in [-0.48, 0.48] at the same distance. Every
    miniature sits to its right, and a raised arm on one must not reach into
    another -- checked with all four on."""
    got = run_js("""
      const vs = g.ghostViews(g.ghostParams("?ghostviews=4"));
      const fk = g.chainFK(chain, {left_shoulder_pitch_joint: -3.0, right_shoulder_pitch_joint: -3.0}); // arms straight up
      const b = g.bones(chain, fk);
      const box = (v) => {
        const m = g.placement(g.m4identity(), v, 1.4);
        let lo = [1e9, 1e9], hi = [-1e9, -1e9];
        for (const s of b) for (const p of [s.a, s.b]) { const q = g.m4point(m, p);
          lo = [Math.min(lo[0], q[0]), Math.min(lo[1], q[1])]; hi = [Math.max(hi[0], q[0]), Math.max(hi[1], q[1])]; }
        return {lo, hi};
      };
      console.log(JSON.stringify(vs.map(box)));
    """)
    for bx in got:
        assert bx["lo"][0] > 0.48, f"a miniature overlaps the camera panel: {bx}"
    for i, a in enumerate(got):
        for b in got[i + 1:]:
            apart_x = a["hi"][0] <= b["lo"][0] + 0.02 or b["hi"][0] <= a["lo"][0] + 0.02
            apart_y = a["hi"][1] <= b["lo"][1] + 0.02 or b["hi"][1] <= a["lo"][1] + 0.02
            assert apart_x or apart_y, f"two miniatures overlap: {a} {b}"


def test_the_knobs_for_both_views_are_documented_in_the_module():
    src = JS.read_text()
    for k in ("ghostpitch", "ghostviews", "ghost2x", "ghost2y", "ghost2yaw", "ghost2pitch",
              "ghost3yaw", "ghost4pitch"):
        assert f'"{k}"' in src, k


def test_every_view_is_drawn_from_one_forward_kinematics():
    """FK once per pose, placement per view: the second copy must not cost a
    second chain walk on a 90 Hz frame loop."""
    src = JS.read_text()
    draw = src.split("export function drawGhost")[1]
    assert draw.count("chainFK(") == 2, "one FK for measured, one for commanded"
    assert "for (const place of places)" in draw


@needs_node
def test_scale_params_moves_and_shrinks_every_copy_together():
    got = run_js("""
      const p = g.ghostParams("?ghostviews=4");
      const s = g.scaleParams(p, 0.8);
      const vs = g.ghostViews(s), v0 = g.ghostViews(p);
      console.log(JSON.stringify({n: vs.length, x: vs.map((v) => v.x), x0: v0.map((v) => v.x),
                                  scale: vs[0].scale, scale0: v0[0].scale, yaw: vs[1].yawDeg, yaw0: v0[1].yawDeg}));
    """)
    assert got["n"] == 4
    assert all(abs(a - 0.8 * b) < 1e-9 for a, b in zip(got["x"], got["x0"]))
    assert abs(got["scale"] - 0.8 * got["scale0"]) < 1e-9
    assert got["yaw"] == got["yaw0"], "angles are not distances"
