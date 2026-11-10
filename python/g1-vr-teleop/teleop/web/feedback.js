// What the headset tells the operator besides the picture.
//
// Three things that used to be silent inside the headset, each one learned
// the hard way from the arm lagging or from nothing at all:
//
// - HAPTICS. The onboard clamps a joint at its calibrated limit, the leash
//   holds one back, or a Dex3 knuckle stalls on what it holds. The bridge already knows every one of
//   these (bridge/server.py, robot_state_message); now the controller on that
//   side buzzes, the way the open Quest teleop kits make a joint limit "feel
//   like a wall". Pure planning here (hapticPlan), the pulse in teleop.js.
// - THE PICTURE'S AGE, drawn on the camera panel, amber past 200 ms and red
//   past 500: so the operator knows whether they are looking at now or at
//   half a second ago before they grab.
//
// And a HEALTH panel: onboard state, hand temperature and fault, camera,
// link -- all of it already on the phone page, none of it visible
// from inside the headset until now.

// ---- haptics -------------------------------------------------------------------

// How hard, per cause.
export const HAPTIC = {
  leashed: 0.4, clamped: 0.7, stall: 0.5,
  pulseMs: 80,        // one short buzz ...
  minGapMs: 150,      // ... at most this often per hand, while the cause holds
  staleMs: 500,       // a message older than this drives nothing
};

function sideOf(joint) {
  return joint.startsWith("left_") ? "left" : joint.startsWith("right_") ? "right" : null;
}

// -> {pulses: [{hand, intensity, ms}], state}. `state` is {last: {left, right}}
// and comes back updated. Only while ARMED: in HOLDING nothing is applied, so
// a "leash" there is the onboard describing a command it is not following.
export function hapticPlan(msg, recvMs, nowMs, state) {
  const st = { last: Object.assign({ left: -1e9, right: -1e9 }, (state && state.last) || {}) };
  const out = { pulses: [], state: st };
  if (!msg || msg.type !== "robot" || nowMs - recvMs > HAPTIC.staleMs) return out;
  if (!String(msg.state || "").startsWith("ARMED")) return out;
  const want = { left: 0, right: 0 };
  const bump = (joint, level) => {
    const side = sideOf(joint);
    if (side) want[side] = Math.max(want[side], level);
  };
  for (const j of msg.leashed || []) bump(j, HAPTIC.leashed);
  for (const j of msg.clamped || []) bump(j, HAPTIC.clamped);
  const hb = msg.hand_backoff || {};
  for (const side of ["left", "right"]) {
    if (Array.isArray(hb[side]) && hb[side].length) want[side] = Math.max(want[side], HAPTIC.stall);
  }
  for (const side of ["left", "right"]) {
    if (want[side] > 0 && nowMs - st.last[side] >= HAPTIC.minGapMs) {
      out.pulses.push({ hand: side, intensity: want[side], ms: HAPTIC.pulseMs });
      st.last[side] = nowMs;
    }
  }
  return out;
}

// ---- the picture's age --------------------------------------------------------------

export const AGE_WARN_MS = 200, AGE_BAD_MS = 500;

// -> {text, level: ok|warn|bad}. `ageMs` null means no frame at all.
export function ageLabel(ageMs) {
  if (ageMs == null || !Number.isFinite(ageMs)) return { text: "no frame", level: "bad" };
  const level = ageMs > AGE_BAD_MS ? "bad" : ageMs > AGE_WARN_MS ? "warn" : "ok";
  return { text: `${Math.round(ageMs)} ms`, level };
}

// The age of the frame on screen: the bridge's age for its newest frame when
// the robot message left, plus how long ago that message arrived.
export function frameAgeMs(msg, recvMs, nowMs) {
  const cam = msg && msg.cam;
  if (!cam || cam.age_s == null) return null;
  return cam.age_s * 1000 + Math.max(0, nowMs - recvMs);
}

const LEVEL_COLOR = { ok: "#9FE2BF", warn: "#F0C33E", bad: "#FF6B6B" };
const SLATE_CHIP = "rgba(23,28,35,0.88)";
const MONO = "\"Geist Mono\", ui-monospace, Menlo, monospace";

// A Geist Mono label on a square slate chip, top-left at (x, y).
function chip(ctx, text, x, y, color) {
  ctx.font = `400 17px ${MONO}`;
  const tw = ctx.measureText(text).width;
  ctx.fillStyle = SLATE_CHIP;
  ctx.fillRect(x, y, tw + 20, 28);
  ctx.fillStyle = color;
  ctx.fillText(text, x + 10, y + 20);
  return tw + 20;
}

// Draw the frame and its age onto a 2D canvas context of size
// w x h. Called at the camera poll rate (4 Hz), not per frame. Wendy's
// brand: square corners, slate chips, Geist Mono labels, seafoam for good.
export function composeCamera(ctx, img, ageMs, w, h) {
  ctx.clearRect(0, 0, w, h);
  if (img) ctx.drawImage(img, 0, 0, w, h);
  const age = ageLabel(ageMs);
  chip(ctx, "G1 CAMERA", 10, 10, "#F1EEE7");
  ctx.font = `400 17px ${MONO}`;
  const aw = ctx.measureText(age.text).width + 20;
  chip(ctx, age.text, w - aw - 10, 10, LEVEL_COLOR[age.level]);
  // A cream hairline frame, so the picture has an edge against the room.
  ctx.strokeStyle = "rgba(241,238,231,0.55)";
  ctx.lineWidth = 2;
  ctx.strokeRect(1, 1, w - 2, h - 2);
  return { age };
}

// ---- health --------------------------------------------------------------------------------

// The Dex3 thermal guard's thresholds (real/dex3_thermal.py: WARM_C, HOT_C).
// tests/test_vr_feedback.py holds these equal to the Python.
export const HAND_WARM_C = 55, HAND_HOT_C = 78;

function shortJoint(name) {
  return String(name || "joint").replace(/_joint$/, "").replace(/^left_/, "L ")
    .replace(/^right_/, "R ").replace(/_/g, " ");
}

// -> [[label, value, level]] from /control/status. Seven rows, one canvas.
export function healthRows(d) {
  d = d || {};
  const rows = [];
  const acked = d.acked || 0, applied = d.applied || 0;
  const pct = acked ? Math.round(100 * applied / acked) : null;
  rows.push(["robot", d.connected === false ? "onboard not connected"
             : `${d.state || "?"}${pct != null ? " · applied " + pct + "%" : ""}`,
             d.connected === false ? "bad" : d.state === "ARMED" ? "ok" : "warn"]);
  const t = d.hand_temp_max_c;
  const handLevel = d.hand_error ? "bad" : t == null ? "warn" : t >= HAND_HOT_C ? "bad"
                  : t >= HAND_WARM_C ? "warn" : "ok";
  rows.push(["hands", `${t != null ? t + " °C" : "no temperature"}${d.hand_error ? " · MOTOR ERROR" : ""}`, handLevel]);
  const motorHot = d.motor_heat_alert || {};
  const thermal = d.thermal || {}, rising = thermal.fastest;
  if (motorHot.active) {
    rows.push(["MOTOR HOT", `${shortJoint(motorHot.joint)} ${motorHot.temperature_c} °C`, "bad"]);
  } else if (rising) {
    rows.push(["heat", `${shortJoint(rising.joint)} +${Number(rising.rise_c_per_s).toFixed(2)} °C/s · ${rising.temperature_c} °C`,
               rising.level === "bad" ? "bad" : "warn"]);
  } else if (thermal.monitored_joints > 0) {
    rows.push(["heat", `${thermal.monitored_joints} joints stable`, "ok"]);
  } else {
    rows.push(["heat", "no joint temperatures", "warn"]);
  }
  const cam = d.camera;
  if (!cam || !cam.connected) rows.push(["camera", "none", "bad"]);
  else {
    const age = ageLabel(cam.last_age_s != null ? cam.last_age_s * 1000 : null);
    rows.push(["camera", `${cam.fps != null ? cam.fps + " fps" : "?"} · ${age.text}`, age.level]);
  }
  const fps = d.xr_fps || 0, tabs = d.xr_clients || 0;
  rows.push(["link", d.xr_connected ? `${Math.round(fps)} fps · ${tabs} tab${tabs === 1 ? "" : "s"}` : "headset not connected",
             !d.xr_connected ? "bad" : (tabs > 1 || fps < 60) ? "warn" : "ok"]);
  rows.push(["arm", `${d.arm_mode || "?"}${d.build && d.build.dirty ? " · DIRTY BUILD" : ""}`,
             d.build && d.build.dirty ? "warn" : "ok"]);
  return rows;
}

// Seven rows at 28 px from y=30. The panel drawing it must keep this ratio or
// the text stretches, as the legend's comment explains.
export const HEALTH_W = 512, HEALTH_H = 220;

export function drawHealth(ctx, rows) {
  ctx.clearRect(0, 0, HEALTH_W, HEALTH_H);
  ctx.fillStyle = "rgba(13, 17, 23, 0.92)";
  ctx.fillRect(0, 0, HEALTH_W, HEALTH_H);
  ctx.font = "bold 21px system-ui, sans-serif";
  let y = 30;
  for (const [label, value, level] of rows) {
    ctx.fillStyle = "#9fb0c4";
    ctx.fillText(label, 18, y);
    ctx.fillStyle = LEVEL_COLOR[level] || "#ffffff";
    ctx.fillText(value, 120, y);
    y += 28;
  }
}
