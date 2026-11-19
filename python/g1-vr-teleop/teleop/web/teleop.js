// G1 VR Teleop — WebXR hand driver (control-out only for milestone 1).
//
// Runs in the Quest 3 browser. Enters an immersive-vr session with hand tracking,
// computes compact per-hand features (wrist pose relative to head + 5 finger curls)
// + head pose, and streams them over a WebSocket to the bridge at frame rate.
//
// The bridge pushes the robot's state back on the same socket (the miniature
// robot and feedback), and the head camera is polled from /camera/frame.jpg.

import * as ghost from "./robot_ghost.js";
import * as fb from "./feedback.js";
import * as hud from "./hud.js";

const $ = (id) => document.getElementById(id);
const statusEl = $("status");
const enterBtn = $("enter");

// WebSocket to the bridge. Served from the same origin, so derive host automatically.
// Over `adb reverse`, this resolves to localhost on the Quest -> your dev machine.
// Must follow the page scheme: an ws:// socket from an https:// page is
// mixed content and is blocked outright, which is indistinguishable from
// the bridge being down.
const WS_SCHEME = location.protocol === "https:" ? "wss" : "ws";
const WS_URL = `${WS_SCHEME}://${location.host}/teleop`;

let ws = null;
let sentCount = 0;

// ---- input source: hands or controllers -------------------------------------
// ?input=hands | controllers | auto (default).
// "auto" takes whatever the headset offers: on Quest, picking the controllers
// up switches the session's input sources from hands to controllers and
// putting them down switches back, so the operator chooses by what they hold.
// The explicit modes ignore the other kind, for when the headset keeps
// offering a source you do not want driving the robot.
const INPUT_MODE = (() => {
  const v = new URLSearchParams(location.search).get("input");
  return v === "hands" || v === "controllers" ? v : "auto";
})();
let lastRateT = 0;
let xrMode = "immersive-vr";
let glRef = null;
let lastArmed = null;

// WebGL resources for the panels, and the robot's head camera.
let camImage = null, camDirty = false;   // the robot's head camera
let fpvGL = null;
// One knob for the whole layout: every panel's and miniature's offset and
// size, times this. ?hudscale=0.8 pulls everything 20 % toward the centre
// and 20 % smaller, at the same distance; 1 is the layout as written in
// drawOverlayView. The default is 1.1: 1.35 was asked for, then judged too
// big once the panels were steady in the room. Clamped, never reverted.
const HUD_SCALE = (() => {
  const v = parseFloat(new URLSearchParams(location.search).get("hudscale"));
  return Number.isFinite(v) ? Math.max(0.4, Math.min(2.0, v)) : 1.1;
})();
// The panels and miniatures stay put in the ROOM rather than following the
// head: placed where the operator faces when the session starts, level
// (heading only, no pitch or roll), and moved only by Quest's own recenter
// (hold the Meta button), which resets the reference space. Head-following
// made the camera swim every time the operator looked at the robot.
// ?hudlock=0 restores the head-locked layout.
const HUD_LOCK = new URLSearchParams(location.search).get("hudlock") !== "0";

// Room-fixed panels go to the Quest COMPOSITOR as quad layers when the
// browser offers WebXR Layers. Drawn into the page's own image, a panel is
// placed with the pose the browser predicted 23-47 ms ahead (VrApi Prd,
// measured on this headset), and every miss moves it against the passthrough
// room -- the operator saw the panels twitch as the head moved, at a steady
// 90 fps. A quad layer is placed by the compositor at each display refresh
// with the newest pose. ?hudlayers=0 keeps the in-page drawing.
const HUD_LAYERS = new URLSearchParams(location.search).get("hudlayers") !== "0";

// Where a panel centred at (x, y, -dist) in the anchor's frame sits in the
// reference space, as a quad layer wants it: position and orientation.
// `k` is HUD_SCALE, applied exactly as panelMvp applies it.
function panelPose(anchor, x, y, dist, k) {
  const lx = x * k, ly = y * k, lz = -dist;
  const a = anchor;
  const yaw = Math.atan2(a[8], a[0]);
  return {
    position: { x: a[0] * lx + a[4] * ly + a[8] * lz + a[12],
                y: a[1] * lx + a[5] * ly + a[9] * lz + a[13],
                z: a[2] * lx + a[6] * ly + a[10] * lz + a[14] },
    orientation: { x: 0, y: Math.sin(yaw / 2), z: 0, w: Math.cos(yaw / 2) },
  };
}

// A room anchor from a head pose: its position and heading, nothing else.
// Column-major, the order WebGL and panelMvp want.
function levelAnchor(head) {
  const q = headYawQuat(head.quat);
  const yaw = 2 * Math.atan2(q[1], q[3]);
  const c = Math.cos(yaw), s = Math.sin(yaw);
  return new Float32Array([c, 0, -s, 0,  0, 1, 0, 0,  s, 0, c, 0,
                           head.pos[0], head.pos[1], head.pos[2], 1]);
}
// Pose sampling and transmission stay at the Quest's XR rate. The optional
// FPV/HUD/ghost is drawn after the pose send. With the panels fixed in the
// room (HUD_LOCK) it is drawn on EVERY XR frame: at the old 30 Hz budget the
// headset showed a stale or empty panel two frames in three, and a
// room-fixed panel then juddered against the room whenever the head moved --
// the operator saw it shake. Texture uploads stay gated on change (camera
// 4 Hz, status 2 Hz), so every frame costs only the quad and mesh draws.
// Head-locked (?hudlock=0) keeps 30 Hz. ?renderhz=0 is every frame;
// 15..90 selects an explicit budget.
const RENDER_HZ = (() => {
  const v = parseFloat(new URLSearchParams(location.search).get("renderhz"));
  if (v === 0) return 0;
  return Number.isFinite(v) ? Math.max(15, Math.min(90, v)) : (HUD_LOCK ? 0 : 30);
})();
const RENDER_INTERVAL_MS = RENDER_HZ > 0 ? 1000 / RENDER_HZ : 0;
// DECLARED HERE, above GHOST, which reads it. `const` is not hoisted: when
// this sat with the other HUD knobs 300 lines below, `const GHOST = ...
// HUD_SCALE)` threw "Cannot access 'HUD_SCALE' before initialization" at
// module load, so teleop.js never ran a line. The page then showed its
// static HTML forever -- the Enter button stuck on "Checking WebXR..." and
// the status on "idle" -- which reads exactly like a headset or certificate
// problem and was chased as one.

// The miniature robot (web/robot_ghost.js): the newest "robot" message from
// the bridge and when it arrived, the chain it is drawn from, its GL state,
// and whether the operator has it shown. ?ghost=0 leaves it off entirely.
let robotMsg = null, robotRecvMs = 0, ghostChain = null, ghostGL = null, ghostMeshes = null;
let ghostState = { shown: true };
const GHOST = ghost.scaleParams(ghost.ghostParams(location.search), HUD_SCALE);
// Feedback (web/feedback.js): controller haptics on limits, the picture's
// age on the camera panel. ?haptics=0
// silences the controllers; the panels stay.
const HAPTICS_ON = new URLSearchParams(location.search).get("haptics") !== "0";
let hapticState = null;
let camCanvas = null, camCtx = null, lastStatus = null;

function log(msg, cls = "") {
  statusEl.innerHTML = `<span class="${cls}">${msg}</span>`;
}

// Put only a NEWEST pose into the browser's socket queue.  WebSocket.send()
// buffers silently; without this gate a weak Wi-Fi interval turns into old
// controller poses arriving in order long after the operator has moved on.
// Dropping a superseded pose is correct for teleoperation: the next XR frame
// is newer and the bridge is latest-wins too.
function sendPoseFrame(ws, payload, stats) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  stats.maxBufferedBytes = Math.max(stats.maxBufferedBytes, ws.bufferedAmount || 0);
  if (ws.bufferedAmount > 0) {
    stats.droppedBackpressure++;
    return false;
  }
  try {
    ws.send(JSON.stringify({...payload, seq: ++stats.seq}));
    stats.poses++;
    return true;
  } catch (e) {
    stats.sendErrors++;
    return false;
  }
}

// ---- tiny math helpers (no deps) -------------------------------------------

function quatInverse(q) { // q = [x,y,z,w], unit quat -> conjugate
  return [-q[0], -q[1], -q[2], q[3]];
}
function quatRotateVec(q, v) {
  // v' = q * v * q^-1 , optimized
  const [x, y, z, w] = q;
  const [vx, vy, vz] = v;
  const tx = 2 * (y * vz - z * vy);
  const ty = 2 * (z * vx - x * vz);
  const tz = 2 * (x * vy - y * vx);
  return [
    vx + w * tx + (y * tz - z * ty),
    vy + w * ty + (z * tx - x * tz),
    vz + w * tz + (x * ty - y * tx),
  ];
}
function sub(a, b) { return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]; }
function quatMul(a, b) { // Hamilton product, [x,y,z,w]
  const [ax, ay, az, aw] = a, [bx, by, bz, bw] = b;
  return [
    aw * bx + ax * bw + ay * bz - az * by,
    aw * by - ax * bz + ay * bw + az * bx,
    aw * bz + ax * by - ay * bx + az * bw,
    aw * bw - ax * bx - ay * by - az * bz,
  ];
}

// WebXR hand joints in spec (index) order. This ordering is deliberate: indices
// 0 / 4 / 9 / 14 = wrist / thumb-tip / index-tip / middle-tip, which is exactly
// what dex-retargeting's unitree_dex3.yml human indices reference.
const WEBXR_JOINTS = [
  "wrist",
  "thumb-metacarpal", "thumb-phalanx-proximal", "thumb-phalanx-distal", "thumb-tip",
  "index-finger-metacarpal", "index-finger-phalanx-proximal", "index-finger-phalanx-intermediate", "index-finger-phalanx-distal", "index-finger-tip",
  "middle-finger-metacarpal", "middle-finger-phalanx-proximal", "middle-finger-phalanx-intermediate", "middle-finger-phalanx-distal", "middle-finger-tip",
  "ring-finger-metacarpal", "ring-finger-phalanx-proximal", "ring-finger-phalanx-intermediate", "ring-finger-phalanx-distal", "ring-finger-tip",
  "pinky-finger-metacarpal", "pinky-finger-phalanx-proximal", "pinky-finger-phalanx-intermediate", "pinky-finger-phalanx-distal", "pinky-finger-tip",
];

// ---- feature extraction per hand -------------------------------------------
// Sends 25 landmarks in the WRIST-LOCAL frame (for dex-retargeting) + the wrist
// pose in the HEAD frame (for arm IK). Wrist-local keeps finger geometry
// orientation-invariant; head-frame wrist gives the arm a torso-anchored target.

// Head heading only -- pitch and roll removed.
//
// Using the FULL head rotation means tilting your head rotates the wrist target
// even though your hands have not moved in the room: look down at the can, and
// the arms swing. Unitree's xr_teleoperate removes pitch and roll for exactly
// this reason. What you want from the head is "which way is the operator
// facing", not "where are they looking".
//
// Done by projection rather than Euler extraction: take the head's forward
// vector (WebXR forward is -Z), flatten it onto the horizontal plane, and
// rebuild a rotation about the up axis. No gimbal case to get wrong when
// someone looks straight up or down.
function headYawQuat(q) {
  const f = quatRotateVec(q, [0, 0, -1]);
  // WebXR is right-handed, +Y up, forward -Z. A head turned by t about +Y has
  // forward (-sin t, 0, -cos t) -- so recovering t needs atan2(-fx, -fz).
  // The first version of this used atan2(fx, -fz), which returns -t and
  // rotated the frame the WRONG WAY BY 2t: with the operator turned 90 deg and
  // a hand held in front of their face, the wrist came out behind the head.
  // It was worse than the full-head code it replaced for any operator not
  // facing where they started. See tests/test_teleop_js.py.
  const yaw = Math.atan2(-f[0], -f[2]);
  return [0, Math.sin(yaw / 2), 0, Math.cos(yaw / 2)];
}

// What an XRInputSource is to us. A tracked hand exposes `hand`; a controller
// exposes a gripSpace and a gamepad. Anything else (a gaze source, a screen
// tap) drives nothing.
function sourceKind(src) {
  if (src.hand) return "hand";
  if (src.gripSpace && src.gamepad) return "controller";
  return null;
}

function sourceAllowed(mode, kind) {
  if (!kind) return false;
  if (mode === "hands") return kind === "hand";
  if (mode === "controllers") return kind === "controller";
  return true;
}

// A controller in place of a tracked hand. Same shape as handFeatures so the
// bridge, recorder and replay treat it as a hand that happens to have no
// landmarks: the grip pose stands in for the wrist, RAW -- the bridge aligns
// the grip frame onto the wrist frame (retarget_g1._GRIP_TO_WRIST), so a
// recording keeps what the headset actually reported. The trigger drives the
// grasp; squeeze, thumbstick and face buttons ride along for bridge controls.
// Face buttons are still sent and recorded raw; none of them drives anything
// now (A/B speech and the held-X wrist gain are both disabled in the bridge).
function controllerFeatures(frame, refSpace, inputSource, head) {
  if (!inputSource.gripSpace || !inputSource.gamepad) return null;
  const pose = frame.getPose(inputSource.gripSpace, refSpace);
  if (!pose) return null;
  const p = pose.transform.position;
  const q = pose.transform.orientation;
  const gpos = [p.x, p.y, p.z];

  // into the head's HEADING frame, exactly as the wrist is
  const hYaw = headYawQuat(head.quat);
  const hInv = quatInverse(hYaw);
  const wristHead = quatRotateVec(hInv, sub(gpos, head.pos));
  const quatHead = quatMul(hInv, [q.x, q.y, q.z, q.w]);

  // xr-standard gamepad mapping: 0 trigger, 1 squeeze, 2/3 thumbstick axes,
  // 4/5 the two face buttons (A/B or X/Y on Touch).
  const gp = inputSource.gamepad;
  const b = (i) => gp.buttons?.[i];
  const ax = gp.axes || [];
  return {
    hand: inputSource.handedness,                     // "left" | "right"
    source: "controller",
    wrist: wristHead.map((n) => +n.toFixed(4)),       // grip origin, head frame
    quat: quatHead.map((n) => +n.toFixed(4)),         // grip orientation, head frame, RAW
    landmarks: null,
    fabricated: 0,
    trigger: +((b(0)?.value ?? 0).toFixed(3)),
    squeeze: +((b(1)?.value ?? 0).toFixed(3)),
    thumbstick: [+((ax[2] ?? 0).toFixed(3)), +((ax[3] ?? 0).toFixed(3))],
    // 3 is the thumbstick CLICK. Both at once toggle arm/home: see
    // arm_on_sticks in bridge/server.py. It needs both hands, which is what
    // makes it deliberate -- this is the control that starts the robot
    // following you.
    buttons: { primary: !!b(4)?.pressed, secondary: !!b(5)?.pressed,
               stick: !!b(3)?.pressed },
  };
}

function handFeatures(frame, refSpace, inputSource, head) {
  const hand = inputSource.hand;
  if (!hand) return null;

  const wristSpace = hand.get("wrist");
  if (!wristSpace) return null;
  const wristPose = frame.getJointPose(wristSpace, refSpace);
  if (!wristPose) return null;

  const wp = wristPose.transform.position;
  const wq = wristPose.transform.orientation;
  const wpos = [wp.x, wp.y, wp.z];
  const wqInv = quatInverse([wq.x, wq.y, wq.z, wq.w]);

  // 25 landmarks in wrist-local frame, in WEBXR_JOINTS order.
  //
  // A joint the headset cannot see is pushed as (0,0,0). These are WRIST-LOCAL,
  // so that is not "missing" -- it is the fingertip AT THE WRIST, i.e. maximum
  // curl, and DexPilot retargets it into a confidently closed finger. Count
  // them so the fabrication is visible instead of silently becoming a grasp.
  const landmarks = [];
  let fabricated = 0;
  for (const name of WEBXR_JOINTS) {
    const space = hand.get(name);
    const pose = space && frame.getJointPose(space, refSpace);
    if (!pose) { landmarks.push(0, 0, 0); fabricated++; continue; }
    const p = pose.transform.position;
    const local = quatRotateVec(wqInv, sub([p.x, p.y, p.z], wpos));
    landmarks.push(+local[0].toFixed(4), +local[1].toFixed(4), +local[2].toFixed(4));
  }

  // wrist pose in the head's HEADING frame (yaw only -- see headYawQuat)
  const hYaw = headYawQuat(head.quat);
  const hInv = quatInverse(hYaw);
  const wristHead = quatRotateVec(hInv, sub(wpos, head.pos));
  const quatHead = quatMul(hInv, [wq.x, wq.y, wq.z, wq.w]);

  return {
    hand: inputSource.handedness,                     // "left" | "right"
    source: "hand",
    wrist: wristHead.map((n) => +n.toFixed(4)),       // meters, head frame
    quat: quatHead.map((n) => +n.toFixed(4)),         // wrist orientation, head frame
    landmarks,                                        // 75 floats = 25 x [x,y,z], wrist-local
    fabricated,                                       // joints the headset could not see
  };
}

// ---- in-headset stereo compositor ------------------------------------------
// Draws each eye's robot-camera frame as a fullscreen quad into that eye's
// viewport, so you look through the G1's eyes. Covers passthrough when a feed
// is present; falls back to passthrough/black when it isn't.

function compileShader(gl, type, src) {
  const s = gl.createShader(type);
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
    log("shader error: " + gl.getShaderInfoLog(s), "err");
    return null;
  }
  return s;
}

function makeTexture(gl) {
  const t = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, t);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  return t;
}

function setupFpvGL(gl) {
  // `rect` is (cx, cy, halfW, halfH) in clip space, so one program draws
  // both the fullscreen feed and the small panels the operator reads.
  const vs = compileShader(gl, gl.VERTEX_SHADER,
    "attribute vec2 pos; varying vec2 uv; uniform vec4 rect;" +
    "void main(){ uv = (pos + 1.0) * 0.5;" +
    "  gl_Position = vec4(rect.xy + pos * rect.zw, 0.0, 1.0); }");
  const fs = compileShader(gl, gl.FRAGMENT_SHADER,
    "precision mediump float; varying vec2 uv; uniform sampler2D tex;" +
    "void main(){ gl_FragColor = texture2D(tex, uv); }");
  if (!vs || !fs) return null;
  const prog = gl.createProgram();
  gl.attachShader(prog, vs); gl.attachShader(prog, fs); gl.linkProgram(prog);
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
  // A second program for the HUD panels: same quad, but placed in the world
  // by a full matrix so each eye gets its own view of it. The rect program
  // above stays for the fullscreen feed, which is screen space by nature.
  const pvs = compileShader(gl, gl.VERTEX_SHADER,
    "attribute vec2 pos; varying vec2 uv; uniform mat4 mvp;" +
    "void main(){ uv = (pos + 1.0) * 0.5; gl_Position = mvp * vec4(pos, 0.0, 1.0); }");
  const pfs = compileShader(gl, gl.FRAGMENT_SHADER,
    "precision mediump float; varying vec2 uv; uniform sampler2D tex;" +
    "void main(){ gl_FragColor = texture2D(tex, uv); }");
  const pprog = gl.createProgram();
  gl.attachShader(pprog, pvs); gl.attachShader(pprog, pfs); gl.linkProgram(pprog);
  return {
    prog, buf,
    aPos: gl.getAttribLocation(prog, "pos"),
    uTex: gl.getUniformLocation(prog, "tex"),
    uRect: gl.getUniformLocation(prog, "rect"),
    texL: makeTexture(gl), texR: makeTexture(gl),
    texHud: makeTexture(gl), texCam: makeTexture(gl), texKeys: makeTexture(gl),
    panel: {
      prog: pprog,
      aPos: gl.getAttribLocation(pprog, "pos"),
      uTex: gl.getUniformLocation(pprog, "tex"),
      uMvp: gl.getUniformLocation(pprog, "mvp"),
    },
  };
}

// ---- the HUD ----------------------------------------------------------------
//
// The operator drives inside a headset and cannot see the laptop, the control
// page, or a list of what the buttons do. Tonight that meant being told which
// button to press by someone reading a terminal, and coming out of VR to ask
// -- and coming out of VR ends the XR session, which drops the socket. So the
// legend goes where they are.
//
// Drawn on a 2D canvas and uploaded as a texture: WebGL has no text, and a
// canvas is redrawn only when something it shows has changed. What it draws,
// and the brand it is drawn in, is web/hud.js.
const HUD_W = hud.HUD_W, HUD_H = hud.HUD_H;           // the controls panel
const STATUS_W = hud.STATUS_W, STATUS_H = hud.STATUS_H;
// Where the panels sit vertically. -0.60 put them at the bottom edge of the
// view and the operator reported not being able to read them; -0.38 is low
// enough to stay out of the way of the robot and high enough to glance at.
//
// Tunable from the URL -- ?hudy=-0.5 -- because how low is comfortable is a
// judgement made while wearing the headset, and a redeploy to nudge a panel
// costs the session: it drops the socket and the operator has to enter VR
// again.
// How far in front the panels sit, in metres. Past arm's reach, which is
// where eyes rest comfortably; much closer and they have to converge hard on
// something they are only glancing at. 1.4 was that, and the operator said
// "too close to my eyes and i dont see fully the edge of the left panels":
// at 1.4 the legend's outer edge sat 39 degrees off axis, past what the
// lenses show sharply. 2.2 now. The panels keep their size in METRES, so
// further also means smaller on the eye (the camera drops from 38 to 25
// degrees wide, the legend's edge to 27 off axis) and closer to the centre,
// which is the point -- less in the face. The miniature robots ride at the
// same distance. ?huddist= to try another.
const HUD_DIST = (() => {
  const v = parseFloat(new URLSearchParams(location.search).get("huddist"));
  return Number.isFinite(v) ? Math.max(0.5, Math.min(4.0, v)) : 2.2;
})();
// How far UP, in METRES at that distance -- not clip space any more, so the
// number means something physical. 0 is eye level.
const HUD_Y = (() => {
  const v = parseFloat(new URLSearchParams(location.search).get("hudy"));
  // CLAMPED, not reverted. Out of range used to fall back to the default
  // silently, so ?hudy=-2 looked exactly like ?hudy= absent -- the operator
  // tried -2 and -5, saw no change, and reasonably concluded the knob did
  // nothing. A value past the end should give you the end.
  //
  // The sign is the other trap: this is clip space, so +1 is UP. -2 was both
  // out of range and the wrong direction for "higher".
  if (!Number.isFinite(v)) return -0.15;
  return Math.max(-1.5, Math.min(1.5, v));
})();
let hudCanvas = null, hudCtx = null, hudDirty = true, hudState = {};
let keysCanvas = null, keysCtx = null;
// The projection layer the page draws into, and the quad layers (HUD_LAYERS)
// when the compositor is placing the panels: {binding, fb, cam, status, anchor}.
let projLayer = null, hudLayers = null, hudResets = 0;
// Geist and the cream logo, fetched once; the panel redraws when they land.
const brand = hud.loadBrand(new URL("./", import.meta.url).href, () => { hudDirty = true; });

function hudText(state) {
  if (!hudCanvas) {
    hudCanvas = document.createElement("canvas");
    hudCanvas.width = STATUS_W; hudCanvas.height = STATUS_H;
    hudCtx = hudCanvas.getContext("2d");
    keysCanvas = document.createElement("canvas");
    keysCanvas.width = HUD_W; keysCanvas.height = HUD_H;
    keysCtx = keysCanvas.getContext("2d");
  }
  hud.drawStatusPanel(hudCtx, state, brand);
  hud.drawControlsPanel(keysCtx, state);
  return hudCanvas;
}

function setHud(next) {
  // Only redraw when something visible changed: this runs in the frame loop.
  for (const k of Object.keys(next)) {
    if (hudState[k] !== next[k]) { hudState = { ...hudState, ...next }; hudDirty = true; return; }
  }
}

// Column-major 4x4 multiply, the order WebGL wants. C = A * B.
function mat4mul(a, b) {
  const o = new Float32Array(16);
  for (let c = 0; c < 4; c++) {
    for (let r = 0; r < 4; r++) {
      o[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1]
                   + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
    }
  }
  return o;
}

// A panel PLACED IN THE WORLD, in front of the head, rather than painted on
// the screen.
//
// Painted identically into both eyes -- which is what the rect path does, and
// what this replaced -- a panel has zero disparity. The eyes are converged on
// something an arm's length away and handed an image that says "infinitely
// far", so it reads as doubled and at the wrong distance. The operator's
// words were: "very close to my eyes... not well aligned for 2 eyes".
//
// Placed at a real distance, each eye is given its own view of it and the
// disparity is correct by construction.
function panelMvp(view, headMatrix, x, y, dist, hw, hh) {
  // model: scale the [-1,1] quad to half-size in metres, put it `dist` in
  // front of the head (-Z is forward in WebXR) and `x`,`y` across and up.
  // All four times HUD_SCALE: the layout is written at scale 1 (the literals
  // in drawOverlay, which the tests read) and shrunk toward the centre here.
  const k = HUD_SCALE;
  const model = new Float32Array([hw * k, 0, 0, 0,  0, hh * k, 0, 0,  0, 0, 1, 0,
                                  x * k, y * k, -dist, 1]);
  return mat4mul(view.projectionMatrix,
                 mat4mul(view.transform.inverse.matrix,
                         mat4mul(headMatrix, model)));
}

function drawPanel(gl, g, tex, mvp) {
  gl.uniformMatrix4fv(g.uMvp, false, mvp);
  gl.activeTexture(gl.TEXTURE0);
  gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.uniform1i(g.uTex, 0);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
}

function drawQuad(gl, g, tex, cx, cy, hw, hh) {
  gl.uniform4f(g.uRect, cx, cy, hw, hh);
  gl.activeTexture(gl.TEXTURE0);
  gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.uniform1i(g.uTex, 0);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
}

// The robot's own camera and the button legend, as two panels.
//
// Panels rather than the fullscreen feed above: the operator is looking at the
// real robot through passthrough, and covering that with a camera image would
// take away the view they are actually driving by. These sit low and to the
// sides, where they can be glanced at.
// Re-upload the status and camera canvases only when they changed. Both the
// in-page panels and the quad layers draw from these two textures.
function uploadHudTextures(gl) {
  if (hudDirty) {
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    gl.bindTexture(gl.TEXTURE_2D, fpvGL.texHud);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, hudText(hudState));
    gl.bindTexture(gl.TEXTURE_2D, fpvGL.texKeys);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, keysCanvas);
    hudDirty = false;
  }
  if (camDirty && camImage) {
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    gl.bindTexture(gl.TEXTURE_2D, fpvGL.texCam);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, camImage);
    camDirty = false;
  }
}

// One eye's view of the three panels, drawn into the page's own image: the
// fallback when the compositor is not placing them (quad layers).
function drawOverlayView(gl, view, vp, hudMatrix) {
  if (!fpvGL || (hudLayers && hudLayers.cam)) return;
  gl.useProgram(fpvGL.prog);
  gl.disable(gl.DEPTH_TEST);
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  const P = fpvGL.panel;
  gl.useProgram(P.prog);
  gl.bindBuffer(gl.ARRAY_BUFFER, fpvGL.buf);
  gl.enableVertexAttribArray(P.aPos);
  gl.vertexAttribPointer(P.aPos, 2, gl.FLOAT, false, 0, 0);
  const head = hudMatrix;
  gl.viewport(vp.x, vp.y, vp.width, vp.height);
  // Camera centered, with room for directions on the left and traces on the right.
  // Keep the full 4:3 sensor image and leave the directions' text size alone.
  if (camImage) {
    drawPanel(gl, P, fpvGL.texCam,
              panelMvp(view, head, 0.0, HUD_Y + 0.12, HUD_DIST, 0.44, 0.33));
  }
  // Directions on the left, clear of the camera (0.82 - 0.32 > 0.44).
  // STATUS above CONTROLS, 0.03 m apart; the stack's midpoint aligns with
  // the camera's center. Each keeps its canvas's aspect and readable size.
  // The white and orange robots are on the camera's other side (GHOST).
  drawPanel(gl, P, fpvGL.texHud,
            panelMvp(view, head, -0.82, HUD_Y + 0.30, HUD_DIST, 0.32, 0.18));
  drawPanel(gl, P, fpvGL.texKeys,
            panelMvp(view, head, -0.82, HUD_Y - 0.075, HUD_DIST, 0.32, 0.165));
  gl.disable(gl.BLEND);
}

// The three panels as compositor quad layers, at the same places and sizes as
// the in-page panels (the literals in drawOverlay). Created once the room
// anchor exists; moved, not recreated, when Quest's recenter resets it.
function hudLayerTransforms(anchor) {
  return {
    cam: panelPose(anchor, 0.0, HUD_Y + 0.12, HUD_DIST, HUD_SCALE),
    status: panelPose(anchor, -0.82, HUD_Y + 0.30, HUD_DIST, HUD_SCALE),
    keys: panelPose(anchor, -0.82, HUD_Y - 0.075, HUD_DIST, HUD_SCALE),
  };
}

function placeHudLayers(session, space, anchor) {
  const t = hudLayerTransforms(anchor);
  if (!hudLayers.cam) {
    // Quest's native quads render width/height as half-extents (verified on
    // OculusBrowser 152). Passing full sizes doubled every panel around its
    // correct center: controls covered status and camera, and camera hid the
    // robots. Three.js createXRLayer uses the same half-size conversion:
    // https://github.com/mrdoob/three.js/blob/dev/src/renderers/common/XRManager.js
    // Keep the standard full-size convention for other WebXR browsers.
    const quadExtent = /OculusBrowser\//.test(navigator.userAgent) ? 1 : 2;
    const make = (pose, hw, hh, w, h) => hudLayers.binding.createQuadLayer({
      space, viewPixelWidth: w, viewPixelHeight: h, layout: "mono",
      transform: new XRRigidTransform(pose.position, pose.orientation),
      width: quadExtent * hw * HUD_SCALE, height: quadExtent * hh * HUD_SCALE,
    });
    const cam = make(t.cam, 0.44, 0.33, 640, 480);
    const status = make(t.status, 0.32, 0.18, STATUS_W, STATUS_H);
    const keys = make(t.keys, 0.32, 0.165, HUD_W, HUD_H);
    // Back to front: the page's own image (robots, passthrough) first.
    session.updateRenderState({ layers: [hudLayers.proj, cam, status, keys] });
    Object.assign(hudLayers, { cam, status, keys });
  } else {
    for (const k of ["cam", "status", "keys"]) {
      hudLayers[k].transform = new XRRigidTransform(t[k].position, t[k].orientation);
    }
  }
  hudLayers.anchor = anchor;
}

// Copy the two canvases' textures into the layers' images. Two quads per
// frame; the uploads behind them only happen when a canvas changed.
function drawHudLayers(gl, frame) {
  if (!hudLayers || !hudLayers.cam) return;
  gl.useProgram(fpvGL.prog);
  gl.disable(gl.DEPTH_TEST);
  gl.disable(gl.BLEND);
  gl.bindBuffer(gl.ARRAY_BUFFER, fpvGL.buf);
  gl.enableVertexAttribArray(fpvGL.aPos);
  gl.vertexAttribPointer(fpvGL.aPos, 2, gl.FLOAT, false, 0, 0);
  gl.bindFramebuffer(gl.FRAMEBUFFER, hudLayers.fb);
  for (const [layer, tex, ready] of [[hudLayers.cam, fpvGL.texCam, !!camImage],
                                     [hudLayers.status, fpvGL.texHud, true],
                                     [hudLayers.keys, fpvGL.texKeys, true]]) {
    const sub = hudLayers.binding.getSubImage(layer, frame);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, sub.colorTexture, 0);
    const vp = sub.viewport;
    gl.viewport(vp.x, vp.y, vp.width, vp.height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    if (ready) drawQuad(gl, fpvGL, tex, 0, 0, 1, 1);
  }
  gl.bindFramebuffer(gl.FRAMEBUFFER, null);
}

// Bind one eye's target in the page's own image and return its viewport.
// With compositor layers that image is an XRProjectionLayer: the old
// XRWebGLLayer is not shown once other layers are in use, which is how the
// white/orange robots went missing. Each view's sub-image is attached to
// our framebuffer with a depth buffer the robots' depth test needs.
function bindView(gl, view) {
  if (hudLayers) {
    const sub = hudLayers.binding.getViewSubImage(hudLayers.proj, view);
    gl.bindFramebuffer(gl.FRAMEBUFFER, hudLayers.viewFb);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, sub.colorTexture, 0);
    const w = hudLayers.proj.textureWidth, h = hudLayers.proj.textureHeight;
    if (!hudLayers.depth || hudLayers.depthW !== w || hudLayers.depthH !== h) {
      hudLayers.depth = hudLayers.depth || gl.createRenderbuffer();
      gl.bindRenderbuffer(gl.RENDERBUFFER, hudLayers.depth);
      gl.renderbufferStorage(gl.RENDERBUFFER, gl.DEPTH_COMPONENT16, w, h);
      hudLayers.depthW = w; hudLayers.depthH = h;
    }
    gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT, gl.RENDERBUFFER, hudLayers.depth);
    return sub.viewport;
  }
  gl.bindFramebuffer(gl.FRAMEBUFFER, projLayer.framebuffer);
  return projLayer.getViewport(view);
}

// Clear only this eye's part: two views can share one texture side by side.
function clearView(gl, vp) {
  gl.enable(gl.SCISSOR_TEST);
  gl.scissor(vp.x, vp.y, vp.width, vp.height);
  gl.clearColor(0, 0, 0, 0);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  gl.disable(gl.SCISSOR_TEST);
}

// ---- session loop -----------------------------------------------------------

async function startSession() {
  log(`requesting ${xrMode} + hand-tracking…`);
  let session;
  try {
    session = await navigator.xr.requestSession(xrMode, {
      requiredFeatures: ["local"],
      optionalFeatures: HUD_LOCK && HUD_LAYERS ? ["hand-tracking", "layers"] : ["hand-tracking"],
    });
  } catch (e) {
    log("session request failed: " + e.message, "err");
    return;
  }

  // alpha:true so AR passthrough shows through where we don't draw.
  const gl = document.createElement("canvas").getContext("webgl", { xrCompatible: true, alpha: true });
  glRef = gl;
  projLayer = null;
  hudLayers = null;
  // Quad layers need the `layers` feature and a binding. Anything short of
  // that -- or a browser that refuses the layers array -- keeps the page's
  // own drawing, which is what ran before; the panels never go missing.
  const layersOK = HUD_LOCK && HUD_LAYERS && typeof XRWebGLBinding === "function"
    && (!session.enabledFeatures || session.enabledFeatures.includes("layers"));
  if (layersOK) {
    try {
      const binding = new XRWebGLBinding(session, gl);
      const proj = binding.createProjectionLayer({ textureType: "texture" });
      session.updateRenderState({ layers: [proj] });
      hudLayers = { binding, proj, fb: gl.createFramebuffer(), viewFb: gl.createFramebuffer(),
                    depth: null, cam: null, status: null, keys: null, anchor: null,
                    quadsFailed: false };
    } catch (e) {
      hudLayers = null;
      log("panels drawn in-page (no compositor layers: " + e.message + ")", "warn");
    }
  }
  if (!hudLayers) {
    projLayer = new XRWebGLLayer(session, gl, { alpha: true });
    session.updateRenderState({ baseLayer: projLayer });
  }
  fpvGL = setupFpvGL(gl);
  if (GHOST.enabled) {
    // Neither may cost the session: without them the page is what it was.
    try { ghostGL = ghost.setupGhostGL(gl); } catch (e) { log("robot miniature off: " + e.message, "warn"); }
    ghost.loadChain().then((c) => { ghostChain = c; })
      .catch((e) => log("robot miniature off: no chain (" + e.message + ")", "warn"));
    // The real shell. 2.5 MB once per session; until it lands, or if it
    // never does, the capsule figure draws from the chain alone.
    ghost.loadMeshes().then((m) => { if (ghostGL) ghostMeshes = ghost.uploadMeshes(gl, m); })
      .catch((e) => log("robot miniature: capsules only (" + e.message + ")", "warn"));
  }
  const refSpace = await session.requestReferenceSpace("local");
  // Where the panels live (HUD_LOCK). Taken from the first tracked head pose,
  // and again after Quest's recenter, which fires `reset` on this space.
  let hudAnchor = null;
  refSpace.addEventListener("reset", () => { hudAnchor = null; hudResets++; });

  // One-second client-side measurements.  A TCP/WebSocket can remain OPEN
  // while WebXR is asleep or a repeated rendering exception prevents pose
  // production.  This heartbeat lets the bridge name those cases instead of
  // showing a misleading decayed "0.1 fps" for all of them.
  const streamStats = {
    raf: 0, poses: 0, cameraFrames: 0, seq: 0, trackedInputs: 0,
    droppedBackpressure: 0, maxBufferedBytes: 0,
    renderErrors: 0, sendErrors: 0,
  };
  let streamWindowT = performance.now();
  const streamHeartbeat = setInterval(() => {
    const now = performance.now();
    const dt = Math.max(0.001, (now - streamWindowT) / 1000);
    const heartbeat = {
      type: "heartbeat",
      t: Math.round(now),
      raf_fps: +(streamStats.raf / dt).toFixed(1),
      pose_fps: +(streamStats.poses / dt).toFixed(1),
      camera_fps: +(streamStats.cameraFrames / dt).toFixed(1),
      tracked_inputs: streamStats.trackedInputs,
      buffered_bytes: Math.max(streamStats.maxBufferedBytes, ws?.bufferedAmount || 0),
      dropped_backpressure: streamStats.droppedBackpressure,
      render_errors: streamStats.renderErrors,
      send_errors: streamStats.sendErrors,
      visibility: document.visibilityState,
      xr_visibility: session.visibilityState || null,
      hud_mode: hudLayers && hudLayers.cam ? "layers" : "page",
      hud_resets: hudResets,
    };
    if (ws && ws.readyState === WebSocket.OPEN && ws.bufferedAmount === 0) {
      try { ws.send(JSON.stringify(heartbeat)); } catch (e) { streamStats.sendErrors++; }
    }
    streamStats.raf = 0;
    streamStats.poses = 0;
    streamStats.cameraFrames = 0;
    streamStats.droppedBackpressure = 0;
    streamStats.maxBufferedBytes = 0;
    streamWindowT = now;
  }, 1000);

  // Reconnect on its own. A dropped socket used to end the run: onclose only
  // logged, and the log is unreadable from inside a headset, so the symptom was
  // "the robot silently stopped following me" with no way back short of taking
  // the headset off and re-entering XR. The USB/adb link is not reliable enough
  // to assume one connection lasts a whole session.
  //
  // Losing the socket is not dangerous by itself: frames stop, the sink stops
  // sending, and the robot freezes on its own. Reconnecting only restores the
  // stream -- it cannot re-arm, which stays a deliberate act on the control page.
  let wsRetry = null;
  const connect = () => {
    ws = new WebSocket(WS_URL);
    ws.onopen = () => {
      log("connected — streaming hands", "ok");
      // Tell the bridge what this headset supports and what the session actually
      // became. Passthrough failures are invisible from the server side
      // otherwise, and unreadable from inside a headset.
      try {
        ws.send(JSON.stringify({
          type: "hello",
          requested: xrMode,
          input: INPUT_MODE,
          environmentBlendMode: session.environmentBlendMode,
          interactionMode: session.interactionMode,
          support: window.__xrSupport || null,
          ua: navigator.userAgent.slice(0, 120),
        }));
      } catch (e) { /* diagnostics must never break the session */ }
    };
    // The return path. The bridge pushes where the robot IS thirty times a
    // second (robot_state_message); anything else on the socket is ignored.
    ws.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data);
        if (m && m.type === "robot") { robotMsg = m; robotRecvMs = performance.now(); }
      } catch (e) { /* not for us */ }
    };
    ws.onerror = () => log("websocket error — is the bridge running?", "err");
    ws.onclose = (e) => {
      // The CODE, not just the fact. A socket that opens, takes the hello and
      // then closes leaves this as the only evidence of who closed it, and
      // without it an evening went into eliminating the server, the
      // certificate, the ports and the browser one at a time.
      //
      //   1000/1001  the page or the session ended it
      //   1006       abnormal: no close frame -- network or browser
      //   1009/1011  the server rejected the message or failed
      log(`websocket closed [${e.code}${e.reason ? " " + e.reason : ""}`
          + `${e.wasClean ? " clean" : " ABNORMAL"}] — reconnecting…`, "warn");
      // Only while the session is live, or we would retry forever after exit.
      if (!wsRetry && session && !sessionEnded) {
        wsRetry = setTimeout(() => { wsRetry = null; connect(); }, 1000);
      }
    };
  };
  // Poll the bridge for what the HUD shows. 2 Hz: it is a legend, not an
  // instrument, and the frame loop must not wait on a fetch.
  const hudPoll = setInterval(async () => {
    try {
      const r = await fetch("/control/status", { cache: "no-store" });
      const d = await r.json();
      lastStatus = d;
      setHud(hud.hudFromStatus(d));
    } catch (e) { /* the legend going stale must not cost the session */ }
  }, 500);

  let sessionEnded = false;

  // The robot's eye, pulled at the sensor's 15 Hz rate. An Image decodes
  // off the main thread and texImage2D takes one directly, so no canvas and no
  // pixel copy in the frame loop. Self-pacing prevents overlapping requests
  // when Wi-Fi or JPEG decoding takes longer than one sensor period.
  const CAM_INTERVAL_MS = 1000 / 15;
  let camPoll = null;
  const pollCamera = () => {
    const started = performance.now();
    const img = new Image();
    const scheduleNext = () => {
      if (sessionEnded) return;
      const delay = Math.max(0, CAM_INTERVAL_MS - (performance.now() - started));
      camPoll = setTimeout(pollCamera, delay);
    };
    img.onload = () => {
      streamStats.cameraFrames++;
      // Composited, not uploaded raw: the frame's age goes on top
      // (feedback.js). One 640x480 draw per decoded camera frame,
      // outside the controller pose loop.
      if (!camCanvas) {
        camCanvas = document.createElement("canvas");
        camCanvas.width = img.naturalWidth || 640; camCanvas.height = img.naturalHeight || 480;
        camCtx = camCanvas.getContext("2d");
      }
      const now = performance.now();
      fb.composeCamera(camCtx, img, fb.frameAgeMs(robotMsg, robotRecvMs, now),
                       camCanvas.width, camCanvas.height);
      camImage = camCanvas; camDirty = true;
      scheduleNext();
    };
    img.onerror = scheduleNext; /* no camera on this robot, or not up yet */
    img.src = `/camera/frame.jpg?t=${Date.now()}`;
  };
  camPoll = setTimeout(pollCamera, 0);

  let lastRenderT = -Infinity;
  session.addEventListener("end", () => {
    sessionEnded = true;
    clearInterval(hudPoll);
    clearTimeout(camPoll);
    clearInterval(streamHeartbeat);
    if (wsRetry) { clearTimeout(wsRetry); wsRetry = null; }
    try { ws && ws.close(); } catch (e) { /* already gone */ }
  });
  connect();

  enterBtn.disabled = true;
  enterBtn.textContent = "In VR";

  session.addEventListener("end", () => {
    enterBtn.textContent = xrMode === "immersive-ar" ? "Enter (passthrough)" : "Enter VR";
  enterBtn.disabled = false;
    enterBtn.textContent = "Enter VR";
    if (ws) ws.close();
    log("session ended", "warn");
  });

  const onFrame = (t, frame) => {
    session.requestAnimationFrame(onFrame);
    streamStats.raf++;

    const viewerPose = frame.getViewerPose(refSpace);
    if (!viewerPose) return;

    const hp = viewerPose.transform.position;
    const hq = viewerPose.transform.orientation;
    const head = { pos: [hp.x, hp.y, hp.z], quat: [hq.x, hq.y, hq.z, hq.w] };
    if (HUD_LOCK && !hudAnchor) hudAnchor = levelAnchor(head);
    const hudMatrix = HUD_LOCK ? hudAnchor : viewerPose.transform.matrix;
    if (hudLayers && !hudLayers.quadsFailed && hudLayers.anchor !== hudAnchor) {
      try { placeHudLayers(session, refSpace, hudAnchor); }
      catch (e) {
        // A refused quad layer never costs the panels: they are drawn into
        // the projection layer instead (drawOverlayView), as before.
        hudLayers.quadsFailed = true;
        hudLayers.cam = hudLayers.status = hudLayers.keys = null;
        try { session.updateRenderState({ layers: [hudLayers.proj] }); } catch (e2) { /* keep what works */ }
        log("panels drawn in-page (quad layer refused: " + e.message + ")", "warn");
      }
    }

    // ---- vestigial dead-man -----------------------------------------------
    // COSMETIC. The bridge never reads frame["armed"], so this gates nothing:
    // it only drives the on-screen message. It is left in place because the
    // controller input path will need exactly this signal, and deleting it
    // would mean writing it again -- but it must not be mistaken for a safety
    // interlock while it is unwired. The real stop is the Unitree remote.
    let deadman = 0;
    for (const src of session.inputSources) {
      const gp = src.gamepad;
      if (!gp) continue;
      // index 0 is the trigger on Quest Touch; fall back to any pressed button
      const trig = gp.buttons?.[0]?.value ?? 0;
      const squeeze = gp.buttons?.[1]?.value ?? 0;
      deadman = Math.max(deadman, trig, squeeze);
    }
    const armed = deadman > 0.5;
    if (armed !== lastArmed) {
      log(armed ? "dead-man HELD — motion enabled" : "dead-man released — frozen",
          armed ? "ok" : "warn");
      lastArmed = armed;
    }

    const hands = [];
    let nHands = 0, nCtrl = 0;
    for (const src of session.inputSources) {
      const kind = sourceKind(src);
      if (!sourceAllowed(INPUT_MODE, kind)) continue;
      const f = kind === "hand" ? handFeatures(frame, refSpace, src, head)
                                : controllerFeatures(frame, refSpace, src, head);
      if (!f) continue;
      hands.push(f);
      if (kind === "hand") nHands++; else nCtrl++;
    }
    streamStats.trackedInputs = hands.length;

    // CONTROL FIRST.  FPV/HUD/ghost rendering is deliberately below this.
    // A cosmetic exception or a slow texture upload must never cost the robot
    // its newest controller pose.
    if (hands.length) {
      const sent = sendPoseFrame(ws, {
        t: Math.round(t),
        armed,                      // dead-man: held trigger, reported every frame
        head: { pos: head.pos.map((n) => +n.toFixed(4)), quat: head.quat.map((n) => +n.toFixed(4)) },
        hands,
      }, streamStats);
      if (sent) sentCount++;
      if (t - lastRateT > 1000) {
        log(`streaming — ${sentCount} fps, ${nHands} hand(s), ${nCtrl} controller(s)` +
            (INPUT_MODE === "auto" ? "" : ` [${INPUT_MODE} only]`), "ok");
        sentCount = 0;
        lastRateT = t;
      }
    }

    // From here down everything is operator feedback, never command
    // production. Skip it at the render budget while pose capture continues
    // on every XR callback above.
    if (t - lastRenderT < RENDER_INTERVAL_MS) return;
    lastRenderT = t;

    // Show or hide the miniature: LEFT stick held alone (ghostToggle). Read
    // here, on the page, because it changes only what the page draws.
    if (GHOST.enabled) {
      let leftStick = false, rightStick = false;
      for (const src of session.inputSources) {
        const pressed = !!src.gamepad?.buttons?.[3]?.pressed;
        if (src.handedness === "left") leftStick = pressed;
        if (src.handedness === "right") rightStick = pressed;
      }
      ghostState = ghost.ghostToggle(ghostState, leftStick, rightStick, t);
      setHud({ ghostShown: ghostState.shown });
    }
    // Haptics: a clamped or leashed joint, a stalled
    // knuckle -- the controller on that side buzzes (feedback.js). The plan
    // is rate-limited there; here only the pulse.
    if (HAPTICS_ON) {
      const plan = fb.hapticPlan(robotMsg, robotRecvMs, performance.now(), hapticState);
      hapticState = plan.state;
      for (const p of plan.pulses) {
        for (const src of session.inputSources) {
          if (src.handedness !== p.hand) continue;
          const act = src.gamepad?.hapticActuators?.[0];
          if (act && act.pulse) { try { act.pulse(p.intensity, p.ms); } catch (e) { /* no actuator */ } }
        }
      }
    }

    // Cosmetic rendering is isolated after the control send.  Keep requesting
    // frames even if one of these optional views fails, and expose the count
    // through the heartbeat rather than relying on an inaccessible console.
    try {
      if (fpvGL) uploadHudTextures(glRef);
      const now = performance.now();
      // One eye at a time: a projection layer hands each view its own
      // sub-image, and the XRWebGLLayer fallback is the same loop.
      for (const view of viewerPose.views) {
        const vp = bindView(glRef, view);
        clearView(glRef, vp);
        drawOverlayView(glRef, view, vp, hudMatrix);
        if (GHOST.enabled && ghostState.shown) {
          ghost.drawGhost(glRef, ghostGL, ghostChain, robotMsg, robotRecvMs, now,
                          [view], { getViewport: () => vp }, hudMatrix, GHOST, HUD_DIST,
                          ghostMeshes);
        }
      }
      // Last: the quad layers use their own framebuffer.
      drawHudLayers(glRef, frame);
    } catch (e) {
      streamStats.renderErrors++;
      if (streamStats.renderErrors === 1 || streamStats.renderErrors % 90 === 0) {
        log(`rendering degraded (${streamStats.renderErrors} errors); controls still streaming`, "warn");
      }
    }
  };
  session.requestAnimationFrame(onFrame);
}

// ---- boot -------------------------------------------------------------------

async function boot() {
  if (!navigator.xr) {
    log("WebXR not available. Open this page in the Quest 3 browser.", "err");
    enterBtn.textContent = "No WebXR";
    return;
  }
  // Prefer AR (passthrough) so the headset shows your real room + Mac screen
  // instead of black — the robot is watched on the Mac in milestone 1.
  // ?mode=ar / ?mode=vr forces a mode, so "passthrough is not working" can be
  // told apart from "this browser reports AR unsupported" without guesswork.
  const forced = new URLSearchParams(location.search).get("mode");
  const arOK = await navigator.xr.isSessionSupported("immersive-ar");
  const vrOK = await navigator.xr.isSessionSupported("immersive-vr");
  window.__xrSupport = {ar: arOK, vr: vrOK, forced: forced || null};
  if (forced === "ar" || forced === "vr") {
    xrMode = forced === "ar" ? "immersive-ar" : "immersive-vr";
  } else if (arOK) {
    xrMode = "immersive-ar";
  } else if (vrOK) {
    xrMode = "immersive-vr";
  } else {
    log("no immersive session supported on this device/browser.", "err");
    enterBtn.textContent = "Unsupported";
    return;
  }
  enterBtn.disabled = false;
  enterBtn.textContent = xrMode === "immersive-ar" ? "Enter (passthrough)" : "Enter VR";
  log(`ready (${xrMode}) — press Enter, enable hand tracking or pick up the controllers.`, "ok");
  enterBtn.addEventListener("click", startSession);
}

boot();

