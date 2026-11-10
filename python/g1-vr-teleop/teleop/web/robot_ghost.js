// The robot, drawn inside the headset.
//
// The head camera looks at the table. At home the arms are at the sides,
// outside its cone, and away from the robot the operator has no idea where the
// hands are until they swing into view. This module draws a miniature of the
// G1's upper body from the bridge's "robot" messages (bridge/server.py,
// robot_state_message): the MEASURED pose solid, the pose the sink actually
// COMMANDED as a translucent ghost. The gap between the two is lag, the leash
// and the arming offset -- everything this repo has so far diagnosed blind.
//
// Every number about the robot comes from web/g1_chain.json, derived from
// the URDF. Nothing
// about the robot's geometry is typed here.
//
// Drawn with plain WebGL, like the panels in teleop.js, from the robot's own
// meshes: the URDF's STLs, decimated and packed into
// web/g1_meshes.bin (2.5 MB, 110k triangles for 37 links) and each link is
// posed by the forward kinematics below. If the meshes are missing or the
// browser cannot index them, capsules between joint origins stand in. Pollen's Reachy app and TelePreview both found a virtual robot
// helps most while learning and while lining up, and is wanted OUT of the way
// otherwise -- so it can be hidden with a held left thumbstick (ghostToggle)
// and left off with ?ghost=0.

// ---- 4x4 matrices, column-major, as WebGL wants them ------------------------

export function m4mul(a, b) {
  const o = new Float32Array(16);
  for (let c = 0; c < 4; c++) {
    for (let r = 0; r < 4; r++) {
      o[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1]
                   + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
    }
  }
  return o;
}

export function m4identity() {
  return new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]);
}

export function m4translate(x, y, z) {
  const m = m4identity();
  m[12] = x; m[13] = y; m[14] = z;
  return m;
}

export function m4scale(x, y, z) {
  const m = m4identity();
  m[0] = x; m[5] = y; m[10] = z;
  return m;
}

// URDF rpy: R = Rz(yaw) * Ry(pitch) * Rx(roll). Same order as
// retarget_g1._urdf_joint_rpy; that function's docstring explains why
// ignoring these is the mistake that cost two finger rotations.
export function m4rpy(r, p, y) {
  const cr = Math.cos(r), sr = Math.sin(r);
  const cp = Math.cos(p), sp = Math.sin(p);
  const cy = Math.cos(y), sy = Math.sin(y);
  // Rz*Ry*Rx written out; columns are the images of x, y, z.
  return new Float32Array([
    cy * cp,                 sy * cp,                 -sp,      0,
    cy * sp * sr - sy * cr,  sy * sp * sr + cy * cr,  cp * sr,  0,
    cy * sp * cr + sy * sr,  sy * sp * cr - cy * sr,  cp * cr,  0,
    0, 0, 0, 1]);
}

// Rotation of `ang` about unit axis `ax` (Rodrigues).
export function m4axisAngle(ax, ang) {
  const [x, y, z] = ax;
  const c = Math.cos(ang), s = Math.sin(ang), t = 1 - c;
  return new Float32Array([
    t * x * x + c,      t * x * y + s * z,  t * x * z - s * y,  0,
    t * x * y - s * z,  t * y * y + c,      t * y * z + s * x,  0,
    t * x * z + s * y,  t * y * z - s * x,  t * z * z + c,      0,
    0, 0, 0, 1]);
}

export function m4point(m, p) {
  return [m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12],
          m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13],
          m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14]];
}

export function m4origin(m) { return [m[12], m[13], m[14]]; }

// ---- forward kinematics -----------------------------------------------------

// Walk the chain root-first. For every joint returns
//   frame: the joint's own frame (parent link, then xyz, then rpy) -- what
//          retarget_g1.forearm_orientation computes for the wrist roll joint
//   link:  the child link's frame (frame, then the joint's rotation)
// `q` maps joint name -> radians; a joint not in it is at zero, which is the
// URDF's zero pose, the same zero the onboard reports against.
export function chainFK(chain, q) {
  const links = { [chain.root_link]: m4identity() };
  const out = {};
  for (const j of chain.joints) {
    const parent = links[j.parent];
    if (!parent) continue;                       // a test chain with a gap
    const frame = m4mul(parent, m4mul(m4translate(...j.xyz), m4rpy(...j.rpy)));
    let link = frame;
    if (j.type !== "fixed") {
      const a = q[j.name] ?? 0;
      if (a) link = m4mul(frame, m4axisAngle(j.axis, a));
    }
    links[j.child] = link;
    out[j.name] = { frame, link };
  }
  return out;
}

// The bridge's vectors, by the orders the chain file carries, into a
// joint-name map. Nulls (a partial command) are simply absent, so they fall
// back to whatever `base` says -- the measured pose, for the ghost.
export function jointMap(chain, arm, waist, hands, base) {
  const q = Object.assign({}, base || {});
  if (Array.isArray(arm)) {
    chain.arm_order.forEach((n, i) => { if (arm[i] != null) q[n] = arm[i]; });
  }
  if (waist != null) q.waist_yaw_joint = waist;
  if (hands) {
    for (const side of ["left", "right"]) {
      const v = hands[side];
      if (!Array.isArray(v)) continue;
      chain.hand_order[side].forEach((n, i) => { if (v[i] != null) q[n] = v[i]; });
    }
  }
  return q;
}

// ---- what to draw, from a message ------------------------------------------

// How old a measurement may be and still be drawn as where the robot IS.
// The onboard replies at frame rate; half a second of silence means it has
// stopped talking, and a frozen arm drawn as live is the one way this display
// could mislead the operator into thinking the robot is following.
export const STALE_S = 0.5;

// Given the newest "robot" message, when it arrived (ms, page clock) and now:
//   solid: draw the measured pose, or false
//   ghost: draw the commanded pose, or false
// A stale message hides BOTH; a fresh message with an old measurement hides
// the solid only -- the bridge is alive, the robot is not answering, and the
// ghost still shows what is being asked of it.
export function ghostVisibility(msg, recvMs, nowMs) {
  if (!msg || msg.type !== "robot") return { solid: false, ghost: false };
  const fresh = (nowMs - recvMs) <= STALE_S * 1000;
  const solid = fresh && Array.isArray(msg.q) && msg.q.length > 0
                && msg.q_age_s != null && msg.q_age_s <= STALE_S;
  const ghost = fresh && !!(msg.cmd && Array.isArray(msg.cmd.arm));
  return { solid, ghost };
}

// Hold the LEFT thumbstick click alone for HOLD_MS to show or hide the
// miniature. Both sticks together is the arming gesture (arm_on_sticks in
// bridge/server.py) and the right stick pressed at any point during the hold
// cancels this one, so a slightly staggered arming press can never toggle
// it. Returns the new state; `st` is {shown, since, fired, cancelled}.
export const HOLD_MS = 600;

export function ghostToggle(st, leftStick, rightStick, tMs) {
  const s = Object.assign({ shown: true, since: null, fired: false, cancelled: false }, st);
  if (!leftStick) {                              // released: rearm
    s.since = null; s.fired = false; s.cancelled = false;
    return s;
  }
  if (rightStick) s.cancelled = true;
  if (s.since == null) s.since = tMs;
  if (!s.fired && !s.cancelled && tMs - s.since >= HOLD_MS) {
    s.shown = !s.shown;
    s.fired = true;                              // once per hold
  }
  return s;
}

// URL knobs, like ?hudy: what is comfortable is judged inside the headset and
// a redeploy to nudge the miniature costs the session. Clamped, never
// reverted -- a value that silently falls back to the default looks exactly
// like a knob that does nothing.
export function ghostParams(search) {
  const p = new URLSearchParams(search || "");
  const num = (k, dflt, lo, hi) => {
    const v = parseFloat(p.get(k));
    return Number.isFinite(v) ? Math.max(lo, Math.min(hi, v)) : dflt;
  };
  return {
    enabled: p.get("ghost") !== "0",
    // View 1: from behind and ABOVE. In the headset, straight from behind
    // the torso hid whichever hand was in front of it ("i dont see the right
    // hand"); looking down puts a forward reach below the shoulders.
    x: num("ghostx", 0.78, -2, 2),             // METRES right of the head axis
    y: num("ghosty", -0.45, -1.5, 1.5),        // METRES up; this is the PELVIS
    scale: num("ghostscale", 0.36, 0.05, 2),   // miniature: 0.36 -> 19 cm of torso
    yawDeg: num("ghostyaw", 0, -180, 180),     // 0 is seen from straight behind
    pitchDeg: num("ghostpitch", 40, -90, 90),  // + tips the head toward you: seen from above
    // View 2: the same robot from its RIGHT side, above view 1. Depth and
    // height of a reach, which the view from behind flattens.
    //
    // Views 3 and 4 (?ghostviews=4): a second column further right -- the
    // LEFT side, and straight down from above. Not on by default: every copy
    // is another ~150k triangles per eye at 90 Hz, and the Quest's frame
    // rate under four has not been measured. One knob away to find out.
    views: num("ghostviews", 2, 1, 4),
    x2: num("ghost2x", 0.78, -2, 2),
    y2: num("ghost2y", -0.10, -1.5, 1.5),
    yaw2Deg: num("ghost2yaw", 90, -180, 180),
    pitch2Deg: num("ghost2pitch", 15, -90, 90),
    x3: num("ghost3x", 1.14, -2, 2),
    y3: num("ghost3y", -0.10, -1.5, 1.5),
    yaw3Deg: num("ghost3yaw", -90, -180, 180),
    pitch3Deg: num("ghost3pitch", 15, -90, 90),
    x4: num("ghost4x", 1.14, -2, 2),
    y4: num("ghost4y", -0.45, -1.5, 1.5),
    yaw4Deg: num("ghost4yaw", 0, -180, 180),
    pitch4Deg: num("ghost4pitch", 80, -90, 90),
  };
}

// The whole layout times `k` (teleop.js HUD_SCALE): every copy's offset and
// size, so ?hudscale= moves the miniatures with the panels.
export function scaleParams(params, k) {
  const out = Object.assign({}, params);
  for (const key of ["x", "y", "x2", "y2", "x3", "y3", "x4", "y4", "scale"]) out[key] = params[key] * k;
  return out;
}

// The placements to draw, from the params: one to four copies of the same
// robot at the same scale.
export function ghostViews(params) {
  const all = [
    { x: params.x, y: params.y, yawDeg: params.yawDeg, pitchDeg: params.pitchDeg },
    { x: params.x2, y: params.y2, yawDeg: params.yaw2Deg, pitchDeg: params.pitch2Deg },
    { x: params.x3, y: params.y3, yawDeg: params.yaw3Deg, pitchDeg: params.pitch3Deg },
    { x: params.x4, y: params.y4, yawDeg: params.yaw4Deg, pitchDeg: params.pitch4Deg },
  ];
  const n = Math.max(1, Math.min(4, Math.round(params.views)));
  return all.slice(0, n).map((v) => Object.assign({ scale: params.scale }, v));
}

// ---- geometry: one cylinder, one sphere, reused for every bone --------------

function unitCylinder(segments) {
  // z from 0 to 1, radius 1, normals radial. Triangle list.
  const pos = [], nrm = [];
  for (let i = 0; i < segments; i++) {
    const a0 = (i / segments) * Math.PI * 2, a1 = ((i + 1) / segments) * Math.PI * 2;
    const c0 = Math.cos(a0), s0 = Math.sin(a0), c1 = Math.cos(a1), s1 = Math.sin(a1);
    pos.push(c0, s0, 0,  c1, s1, 0,  c1, s1, 1,   c0, s0, 0,  c1, s1, 1,  c0, s0, 1);
    nrm.push(c0, s0, 0,  c1, s1, 0,  c1, s1, 0,   c0, s0, 0,  c1, s1, 0,  c0, s0, 0);
  }
  return { pos: new Float32Array(pos), nrm: new Float32Array(nrm), n: pos.length / 3 };
}

function unitSphere(lon, lat) {
  const pos = [], nrm = [];
  const P = (i, j) => {
    const th = (i / lon) * Math.PI * 2, ph = (j / lat) * Math.PI;
    return [Math.cos(th) * Math.sin(ph), Math.sin(th) * Math.sin(ph), Math.cos(ph)];
  };
  for (let i = 0; i < lon; i++) {
    for (let j = 0; j < lat; j++) {
      const a = P(i, j), b = P(i + 1, j), c = P(i + 1, j + 1), d = P(i, j + 1);
      for (const v of [a, b, c, a, c, d]) { pos.push(...v); nrm.push(...v); }
    }
  }
  return { pos: new Float32Array(pos), nrm: new Float32Array(nrm), n: pos.length / 3 };
}

// ---- the bones: which joint origins to connect, and how thick ----------------
//
// Radii are STYLING -- how fat a stick figure looks -- not robot geometry, and
// the one place a number is chosen here. Lengths and positions all come from
// the chain. Fingertips use the terminal link's mesh extent: the joint tree
// ends at the last joint, and the mesh is the only honest source of the
// length beyond it.

const ARM = ["shoulder_pitch", "shoulder_roll", "shoulder_yaw", "elbow",
             "wrist_roll", "wrist_pitch", "wrist_yaw"];
const ARM_R = [0.04, 0.036, 0.034, 0.03, 0.026, 0.022, 0.02];
const FINGERS = [["thumb_0", "thumb_1", "thumb_2"], ["index_0", "index_1"], ["middle_0", "middle_1"]];
const FINGER_R = 0.009;

// The point on a terminal link farthest from its joint along the link's
// dominant axis -- the fingertip -- from the mesh extent, in the link frame.
export function tipOf(extent) {
  const [lo, hi] = extent;
  let best = 0, bestK = 0;
  for (let k = 0; k < 3; k++) {
    const v = Math.abs(lo[k]) > Math.abs(hi[k]) ? lo[k] : hi[k];
    if (Math.abs(v) > Math.abs(best)) { best = v; bestK = k; }
  }
  const p = [0, 0, 0];
  p[bestK] = best;
  return p;
}

// -> [{a:[x,y,z], b:[x,y,z], r}] in the pelvis frame for this pose.
// `armsOnly` leaves out the torso and head: the ghost is the COMMAND, and
// the command is arms, hands and waist -- a second torso drawn over the first
// only muddies both.
export function bones(chain, fk, armsOnly = false) {
  const out = [];
  const seg = (a, b, r) => { if (a && b) out.push({ a, b, r }); };
  const at = (name) => fk[name] && m4origin(fk[name].frame);
  const linkOf = (name) => fk[name] && fk[name].link;

  // Torso: from the waist pitch joint up through the torso mesh's extent,
  // then the head as a ball where its mesh sits. Drawn in the torso link's
  // frame so a turned waist turns them.
  const torso = armsOnly ? null : linkOf("waist_pitch_joint");
  const tx = chain.links.torso_link?.extent, hx = chain.links.head_link?.extent;
  if (torso && tx) {
    const cx = (tx[0][0] + tx[1][0]) / 2, cy = (tx[0][1] + tx[1][1]) / 2;
    seg(m4point(torso, [cx, cy, tx[0][2]]), m4point(torso, [cx, cy, tx[1][2]]),
        0.55 * (tx[1][1] - tx[0][1]) / 2);
  }
  if (torso && hx) {
    const c = [(hx[0][0] + hx[1][0]) / 2, (hx[0][1] + hx[1][1]) / 2, (hx[0][2] + hx[1][2]) / 2];
    const r = Math.min(hx[1][0] - hx[0][0], hx[1][1] - hx[0][1], hx[1][2] - hx[0][2]) / 2;
    const p = m4point(torso, c);
    out.push({ a: p, b: p, r });
  }

  for (const side of ["left", "right"]) {
    const names = ARM.map((j) => `${side}_${j}_joint`);
    for (let i = 0; i + 1 < names.length; i++) seg(at(names[i]), at(names[i + 1]), ARM_R[i]);
    const palm = `${side}_hand_palm_joint`;
    seg(at(names[names.length - 1]), at(palm), ARM_R[ARM_R.length - 1]);
    const palmLink = linkOf(palm), px = chain.links[`${side}_hand_palm_link`]?.extent;
    if (palmLink && px) {
      const tip = tipOf(px);
      seg(at(palm), m4point(palmLink, tip), (px[1][1] - px[0][1]) / 2);
    }
    for (const finger of FINGERS) {
      const fn = finger.map((f) => `${side}_hand_${f}_joint`);
      seg(at(palm), at(fn[0]), FINGER_R);
      for (let i = 0; i + 1 < fn.length; i++) seg(at(fn[i]), at(fn[i + 1]), FINGER_R);
      const last = fn[fn.length - 1];
      const ext = chain.links[`${side}_hand_${finger[finger.length - 1]}_link`]?.extent;
      const ll = linkOf(last);
      if (ll && ext) seg(at(last), m4point(ll, tipOf(ext)), FINGER_R);
    }
  }
  return out;
}

// Robot pelvis frame (X forward, Y left, Z up) into WebXR (X right, Y up,
// -Z forward), so the miniature FACES AWAY from the operator: its left is the
// operator's left, as in a mirror room seen from behind. The inverse of
// retarget_g1._R_XR_TO_ROBOT, and tests/test_vr_ghost.py holds them equal.
export const ROBOT_TO_XR = new Float32Array([
  0, 0, -1, 0,     // robot X (forward) -> -Z_xr
 -1, 0,  0, 0,     // robot Y (left)    -> -X_xr
  0, 1,  0, 0,     // robot Z (up)      ->  Y_xr
  0, 0,  0, 1]);

// ---- WebGL --------------------------------------------------------------------

const VS = `
attribute vec3 pos; attribute vec3 nrm;
uniform mat4 mvp; uniform mat4 model; uniform vec3 scale;
varying vec3 vN;
void main() {
  gl_Position = mvp * vec4(pos * scale, 1.0);
  // A bone is a unit cylinder stretched to (r, r, length): normals scale by
  // the inverse, then rotate with the model.
  vN = mat3(model) * normalize(nrm / scale);
}`;
const FS = `
precision mediump float;
uniform vec4 color; uniform vec3 light;
varying vec3 vN;
void main() {
  // Two-sided: abs, not max. The decimated shells carry a few faces whose
  // winding the clustering flipped, and one-sided lighting drew them as
  // dark speckles all over the torso.
  float l = 0.35 + 0.65 * abs(dot(normalize(vN), normalize(light)));
  gl_FragColor = vec4(color.rgb * l, color.a);
}`;

function compile(gl, type, src) {
  const s = gl.createShader(type);
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
    throw new Error("ghost shader: " + gl.getShaderInfoLog(s));
  }
  return s;
}

function upload(gl, mesh) {
  const pos = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, pos);
  gl.bufferData(gl.ARRAY_BUFFER, mesh.pos, gl.STATIC_DRAW);
  const nrm = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, nrm);
  gl.bufferData(gl.ARRAY_BUFFER, mesh.nrm, gl.STATIC_DRAW);
  return { pos, nrm, n: mesh.n };
}

export function setupGhostGL(gl) {
  const prog = gl.createProgram();
  gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, VS));
  gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, FS));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    throw new Error("ghost program: " + gl.getProgramInfoLog(prog));
  }
  return {
    prog,
    aPos: gl.getAttribLocation(prog, "pos"),
    aNrm: gl.getAttribLocation(prog, "nrm"),
    uMvp: gl.getUniformLocation(prog, "mvp"),
    uModel: gl.getUniformLocation(prog, "model"),
    uScale: gl.getUniformLocation(prog, "scale"),
    uColor: gl.getUniformLocation(prog, "color"),
    uLight: gl.getUniformLocation(prog, "light"),
    cyl: upload(gl, unitCylinder(12)),
    sph: upload(gl, unitSphere(10, 6)),
  };
}

// Rigid transform taking the unit z axis onto b-a, origin at a.
function boneFrame(a, b) {
  const d = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
  const len = Math.hypot(d[0], d[1], d[2]);
  if (len < 1e-9) return { m: m4translate(...a), len: 0 };
  const z = d.map((v) => v / len);
  // any vector not parallel to z
  const up = Math.abs(z[1]) < 0.9 ? [0, 1, 0] : [1, 0, 0];
  const x = [up[1] * z[2] - up[2] * z[1], up[2] * z[0] - up[0] * z[2], up[0] * z[1] - up[1] * z[0]];
  const xl = Math.hypot(...x);
  const xn = x.map((v) => v / xl);
  const y = [z[1] * xn[2] - z[2] * xn[1], z[2] * xn[0] - z[0] * xn[2], z[0] * xn[1] - z[1] * xn[0]];
  return { m: new Float32Array([xn[0], xn[1], xn[2], 0, y[0], y[1], y[2], 0,
                                z[0], z[1], z[2], 0, a[0], a[1], a[2], 1]), len };
}

function bind(gl, G, mesh) {
  gl.bindBuffer(gl.ARRAY_BUFFER, mesh.pos);
  gl.enableVertexAttribArray(G.aPos);
  gl.vertexAttribPointer(G.aPos, 3, gl.FLOAT, false, 0, 0);
  gl.bindBuffer(gl.ARRAY_BUFFER, mesh.nrm);
  gl.enableVertexAttribArray(G.aNrm);
  gl.vertexAttribPointer(G.aNrm, 3, gl.FLOAT, false, 0, 0);
}

// Where a miniature sits: in front of the head like the panels, so it is a
// glance away wherever the operator looks. `place` is head * translate *
// pitch * yaw * scale * ROBOT_TO_XR, and the pelvis is what lands at
// (x, y, -dist). Pitch is about the headset's X AFTER the yaw, so it always
// tips the robot toward or away from the viewer -- positive brings the head
// toward you, which is the view from above and behind.
export function placement(headMatrix, params, dist) {
  const yaw = m4axisAngle([0, 1, 0], params.yawDeg * Math.PI / 180);
  const pitch = m4axisAngle([1, 0, 0], (params.pitchDeg || 0) * Math.PI / 180);
  const s = params.scale;
  return m4mul(headMatrix,
         m4mul(m4translate(params.x, params.y, -dist),
         m4mul(pitch, m4mul(yaw, m4mul(m4scale(s, s, s), ROBOT_TO_XR)))));
}

// Draw one pose's bones for one eye. `viewProj` = projection * view^-1.
export function drawBones(gl, G, boneList, viewProj, place, color) {
  gl.uniform4fv(G.uColor, color);
  for (const bone of boneList) {
    const { m, len } = boneFrame(bone.a, bone.b);
    const model = m4mul(place, m);
    gl.uniformMatrix4fv(G.uModel, false, model);
    gl.uniformMatrix4fv(G.uMvp, false, m4mul(viewProj, model));
    if (len > 0) {
      gl.uniform3f(G.uScale, bone.r, bone.r, len);
      bind(gl, G, G.cyl);
      gl.drawArrays(gl.TRIANGLES, 0, G.cyl.n);
    }
    // a ball at each end rounds the joint and caps the cylinder
    gl.uniform3f(G.uScale, bone.r, bone.r, bone.r);
    bind(gl, G, G.sph);
    gl.drawArrays(gl.TRIANGLES, 0, G.sph.n);
    if (len > 0) {
      const end = m4mul(place, m4translate(...bone.b));
      gl.uniformMatrix4fv(G.uModel, false, end);
      gl.uniformMatrix4fv(G.uMvp, false, m4mul(viewProj, end));
      gl.drawArrays(gl.TRIANGLES, 0, G.sph.n);
    }
  }
}

// ---- the real meshes ----------------------------------------------------------

// Split the packed binary by the manifest's offsets. Pure, so it can run under
// node: -> {link: {pos: Float32Array, nrm: Float32Array, idx: Uint32Array,
// origin: mat4, scale: [3]}}.
export function parseMeshes(manifest, buffer) {
  if (!manifest || manifest.schema !== "wendy.g1.meshes.v1") throw new Error("meshes schema");
  if (buffer.byteLength !== manifest.bin_bytes) {
    throw new Error(`meshes: ${buffer.byteLength} bytes, manifest says ${manifest.bin_bytes}`);
  }
  const out = {};
  for (const [name, e] of Object.entries(manifest.links)) {
    out[name] = {
      pos: new Float32Array(buffer, e.pos_offset, e.verts * 3),
      nrm: new Float32Array(buffer, e.nrm_offset, e.verts * 3),
      idx: new Uint32Array(buffer, e.idx_offset, e.tris * 3),
      origin: m4mul(m4translate(...e.origin_xyz), m4rpy(...e.origin_rpy)),
      scale: e.scale,
      tris: e.tris,
    };
  }
  return out;
}

export async function loadMeshes(url = "/g1_meshes.json") {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`meshes ${r.status}`);
  const manifest = await r.json();
  const b = await fetch(url.replace(/[^/]*$/, manifest.bin), { cache: "no-store" });
  if (!b.ok) throw new Error(`meshes bin ${b.status}`);
  return parseMeshes(manifest, await b.arrayBuffer());
}

// Upload once; the buffers live for the GL context. Needs 32-bit indices
// (OES_element_index_uint; every Quest browser has it) -- without it, null,
// and the capsules draw.
export function uploadMeshes(gl, meshes) {
  if (!gl.getExtension("OES_element_index_uint")) return null;
  const out = {};
  for (const [name, m] of Object.entries(meshes)) {
    const pos = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, pos);
    gl.bufferData(gl.ARRAY_BUFFER, m.pos, gl.STATIC_DRAW);
    const nrm = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, nrm);
    gl.bufferData(gl.ARRAY_BUFFER, m.nrm, gl.STATIC_DRAW);
    const idx = gl.createBuffer();
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, idx);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, m.idx, gl.STATIC_DRAW);
    out[name] = { pos, nrm, idx, n: m.idx.length, origin: m.origin, scale: m.scale };
  }
  return out;
}

// Which links the ghost draws: what is commanded -- arms and hands. The
// torso is drawn once, from the measured pose.
export function isArmLink(name) {
  return /^(left|right)_(shoulder|elbow|wrist|hand)/.test(name);
}

// The child link of every joint in the chain, posed: {link name: mat4}.
export function linkPoses(chain, fk) {
  const out = {};
  for (const j of chain.joints) if (fk[j.name]) out[j.child] = fk[j.name].link;
  return out;
}

// Draw every uploaded link at its pose for one eye.
export function drawMeshes(gl, G, gpu, poses, viewProj, place, color, armsOnly) {
  gl.uniform4fv(G.uColor, color);
  for (const [name, m] of Object.entries(gpu)) {
    const T = poses[name];
    if (!T || (armsOnly && !isArmLink(name))) continue;
    const model = m4mul(place, m4mul(T, m.origin));
    gl.uniformMatrix4fv(G.uModel, false, model);
    gl.uniformMatrix4fv(G.uMvp, false, m4mul(viewProj, model));
    gl.uniform3f(G.uScale, m.scale[0], m.scale[1], m.scale[2]);
    gl.bindBuffer(gl.ARRAY_BUFFER, m.pos);
    gl.enableVertexAttribArray(G.aPos);
    gl.vertexAttribPointer(G.aPos, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, m.nrm);
    gl.enableVertexAttribArray(G.aNrm);
    gl.vertexAttribPointer(G.aNrm, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, m.idx);
    gl.drawElements(gl.TRIANGLES, m.n, gl.UNSIGNED_INT, 0);
  }
}

// The pelvis link is the root and has no joint above it in the chain; it
// sits at the origin, which is also where the URDF puts it.
export function rootPose(chain) { return { [chain.root_link]: m4identity() }; }

export const SOLID_COLOR = [0.55, 0.78, 0.98, 1.0];    // measured: the robot
export const MESH_COLOR = [0.95, 0.93, 0.90, 1.0];     // the G1's shell, lit: Wendy cream
export const GHOST_COLOR = [0.99, 0.62, 0.30, 0.45];   // commanded: where it was told to go
export const STALE_COLOR = [0.5, 0.5, 0.5, 0.35];      // no measurement: hint only

// The whole thing for one frame. `views` are the XR views, `headMatrix` the
// viewer pose, `msg`/`recvMs` the newest robot message and when it came.
// `gpu` is uploadMeshes()' result or null; with it the real meshes draw,
// without it the capsules.
export function drawGhost(gl, G, chain, msg, recvMs, nowMs, views, layer, headMatrix, params, dist, gpu = null) {
  if (!G || !chain) return null;
  const vis = ghostVisibility(msg, recvMs, nowMs);
  if (!vis.solid && !vis.ghost) return vis;
  const measured = jointMap(chain, vis.solid ? msg.q : null, vis.solid ? msg.waist : null,
                            vis.solid ? msg.hand_q : null, null);
  const solidFK = vis.solid ? chainFK(chain, measured) : null;
  const ghostFK = vis.ghost
    ? chainFK(chain, jointMap(chain, msg.cmd.arm, msg.cmd.waist, msg.cmd.hands, measured))
    : null;
  // The torso once, from the measured pose when there is one.
  const armsOnly = !!solidFK;
  const solidBones = solidFK && !gpu ? bones(chain, solidFK) : null;
  const ghostBones = ghostFK && !gpu ? bones(chain, ghostFK, armsOnly) : null;
  const solidPoses = solidFK && gpu ? Object.assign(rootPose(chain), linkPoses(chain, solidFK)) : null;
  const ghostPoses = ghostFK && gpu ? Object.assign(rootPose(chain), linkPoses(chain, ghostFK)) : null;
  const places = ghostViews(params).map((v) => placement(headMatrix, v, dist));

  gl.useProgram(G.prog);
  gl.enable(gl.DEPTH_TEST);
  gl.depthFunc(gl.LEQUAL);
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  gl.uniform3f(G.uLight, 0.3, 0.8, 0.5);
  for (const view of views) {
    const vp = layer.getViewport(view);
    gl.viewport(vp.x, vp.y, vp.width, vp.height);
    const viewProj = m4mul(view.projectionMatrix, view.transform.inverse.matrix);
    const gc = (solidBones || solidPoses) ? GHOST_COLOR : STALE_COLOR;
    for (const place of places) {
      if (solidBones) drawBones(gl, G, solidBones, viewProj, place, SOLID_COLOR);
      if (solidPoses) drawMeshes(gl, G, gpu, solidPoses, viewProj, place, MESH_COLOR, false);
      // Ghost after solid so it blends over it where they coincide, and
      // without writing depth: a translucent arm must not hide the real one.
      gl.depthMask(false);
      if (ghostBones) drawBones(gl, G, ghostBones, viewProj, place, gc);
      if (ghostPoses) drawMeshes(gl, G, gpu, ghostPoses, viewProj, place, gc, armsOnly);
      gl.depthMask(true);
    }
  }
  gl.disable(gl.DEPTH_TEST);
  return vis;
}

export async function loadChain(url = "/g1_chain.json") {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`chain ${r.status}`);
  const chain = await r.json();
  if (chain.schema !== "wendy.g1.chain.v1") throw new Error(`chain schema ${chain.schema}`);
  return chain;
}
