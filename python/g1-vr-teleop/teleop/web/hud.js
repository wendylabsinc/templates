// The headset's status panel, in Wendy's brand (https://wendy.dev/brand).
//
// One panel beside the camera, not the legend + health pair it replaced: the
// operator asked for the camera, the white/orange robots, whether it is ARMED
// and how to drive it -- nothing else competing for a glance. Safety alerts
// (a hot motor, a hand fault, the robot gone) still interrupt, as
// lines under the state; everything else lives on the laptop.
//
// A module of its own so a plain browser tab can draw it:
// teleop.js only runs inside an XR session, and a panel judged only from
// inside the headset is judged by someone who cannot also look at the code.
//
// Brand: slate surfaces (never black), cream ink, seafoam only for the one
// thing that matters (ARMED), Geist 500 for words and Geist Mono uppercase
// for labels, square corners.

export const BRAND = {
  slate: "#171C23", card: "#1E242D", muted: "#242B35",
  cream: "#F1EEE7", sand: "#E6E2D8", key: "#C9C5BC",
  seafoam: "#9FE2BF",
  // Not brand colours: safety. A warning has to read as one.
  warn: "#F0C33E", bad: "#FF6B6B",
};
export const FONT = "Geist, ui-sans-serif, system-ui, sans-serif";
export const MONO = "\"Geist Mono\", ui-monospace, Menlo, monospace";

// Two panels, stacked beside the camera: STATUS on top (the logo, ARMED,
// alerts) and CONTROLS below, separate by request. Each canvas keeps its
// panel's aspect in teleop.js or the text stretches -- and a stretched
// legend reads as a blurry one:
//   status   512x288 <-> 0.32 x 0.18 half-extents
//   controls 512x264 <-> 0.32 x 0.165
// Together, with a 0.03 m gap, they are centered beside the smaller camera.
export const STATUS_W = 512, STATUS_H = 288;
export const HUD_W = 512, HUD_H = 264;

// Load Geist and the cream logo once. The panel draws with fallbacks until
// they land, then `onReady` asks for one redraw. Every step is guarded: this
// runs at module load, and a module that throws is a page stuck on
// "Checking WebXR..." (tests/test_teleop_module_loads.py).
export function loadBrand(base, onReady) {
  const assets = { logo: null };
  const done = () => { try { onReady && onReady(); } catch (e) { /* cosmetic */ } };
  try {
    const faces = [new FontFace("Geist", `url(${base}geist-variable.woff2)`, { weight: "100 900" }),
                   new FontFace("Geist Mono", `url(${base}geist-mono-variable.woff2)`, { weight: "100 900" })];
    for (const f of faces) f.load().then((l) => { document.fonts.add(l); done(); }).catch(() => {});
  } catch (e) { /* no FontFace: system fonts */ }
  try {
    const img = new Image();
    img.onload = () => { assets.logo = img; done(); };
    img.src = `${base}wendy-logo-cream.svg`;
  } catch (e) { /* no Image: the panel draws without the logo */ }
  return assets;
}

// /control/status -> what the panel shows. Pure, so the tests can hold it.
export function hudFromStatus(d) {
  d = d || {};
  const alerts = [];
  const hot = d.motor_heat_alert || {};
  if (hot.active) {
    const j = String(hot.joint || "motor").replace(/_joint$/, "").replace(/_/g, " ");
    alerts.push([`Motor hot: ${j} ${hot.temperature_c} °C`, "bad"]);
  }
  if (d.hand_error) alerts.push(["Hand motor error", "bad"]);
  return {
    state: String(d.state || "—").split(" ")[0],
    connected: d.connected !== false,
    homing: !!d.homing,
    recording: !!(d.session && d.session.recording),
    // TELEOP_RECORDING=0 hides the squeeze row: there is nothing to record.
    recordingEnabled: d.recording_enabled !== false,
    waistFrozen: !!d.waist_frozen,
    locomotion: d.locomotion || null,
    walkingAllowed: d.locomotion_permission?.allowed === true,
    alerts: alerts.slice(0, 2).map((a) => a.join("|")).join("\n"),
  };
}

// -> {word, line, tone}. tone: armed | idle | busy | bad.
export function stateView(state) {
  if (state.connected === false) return { word: "OFFLINE", line: "Robot not connected", tone: "bad" };
  if (state.homing) return { word: "RETURNING HOME", line: "Hands ignored until it arrives", tone: "busy" };
  const s = state.state || "—";
  if (s === "ARMED") return { word: "ARMED", line: "Robot follows your hands", tone: "armed" };
  if (s === "HOLDING" || s === "STANDBY") return { word: s, line: "Click both sticks to arm", tone: "idle" };
  return { word: s, line: "", tone: "idle" };
}

// Shrink a font until `text` fits `maxW`. Canvas has no text wrapping, and a
// word cut at the panel edge is a word the operator has to guess.
function fit(c, text, weight, size, family, maxW) {
  let px = size;
  c.font = `${weight} ${px}px ${family}`;
  while (px > 10 && c.measureText(text).width > maxW) {
    px -= 1;
    c.font = `${weight} ${px}px ${family}`;
  }
}

function hairline(c, y, w) {
  c.fillStyle = "rgba(241,238,231,0.14)";
  c.fillRect(28, y, w - 56, 1);
}

export function drawStatusPanel(c, state, assets) {
  const W = STATUS_W, H = STATUS_H, X = 28;
  c.clearRect(0, 0, W, H);
  // Nearly opaque: the panel is blended over PASSTHROUGH -- a lit room --
  // and every percent of transparency is room light washing the text out.
  c.fillStyle = "rgba(23,28,35,0.96)";
  c.fillRect(0, 0, W, H);
  c.textBaseline = "alphabetic";

  // Header: the logo, and what this is.
  const logo = assets && assets.logo;
  if (logo) c.drawImage(logo, X, 26, 30 * 749.97 / 181.81, 30);
  c.font = `400 15px ${MONO}`;
  c.fillStyle = BRAND.key;
  const tag = "G1 TELEOP";
  c.fillText(tag, W - X - c.measureText(tag).width, 48);
  hairline(c, 76, W);

  // State first and largest: the one thing worth reading at a glance is
  // whether the robot is following you.
  const v = stateView(state);
  c.font = `400 15px ${MONO}`;
  c.fillStyle = BRAND.key;
  c.fillText("STATUS", X, 108);
  const ink = v.tone === "armed" ? BRAND.seafoam : v.tone === "bad" ? BRAND.bad
            : v.tone === "busy" ? BRAND.warn : BRAND.cream;
  // The square: filled while the robot follows you, an outline otherwise.
  c.fillStyle = ink;
  if (v.tone === "armed" || v.tone === "bad") c.fillRect(X, 138, 20, 20);
  else { c.fillRect(X, 138, 20, 2); c.fillRect(X, 156, 20, 2); c.fillRect(X, 138, 2, 20); c.fillRect(X + 18, 138, 2, 20); }
  fit(c, v.word, 500, 56, FONT, W - X - 64);
  c.fillText(v.word, X + 36, 162);
  c.font = `500 22px ${FONT}`;
  c.fillStyle = BRAND.sand;
  c.fillText(v.line, X, 198);

  let ay = 232;
  for (const line of String(state.alerts || "").split("\n").filter(Boolean)) {
    const [text, level] = line.split("|");
    c.fillStyle = level === "bad" ? BRAND.bad : BRAND.warn;
    c.fillRect(X, ay - 14, 4, 18);
    fit(c, text, 500, 20, FONT, W - X * 2 - 14);
    c.fillText(text, X + 14, ay);
    ay += 26;
  }
}

// The controls, a panel of their own under the status.
export function drawControlsPanel(c, state) {
  const W = HUD_W, H = HUD_H, X = 28;
  c.clearRect(0, 0, W, H);
  c.fillStyle = "rgba(23,28,35,0.96)";
  c.fillRect(0, 0, W, H);
  c.textBaseline = "alphabetic";
  const armed = stateView(state).tone === "armed";
  const rows = [
    ["BOTH sticks", state.homing ? "Returning home…"
      : (armed ? "Hold + go home" : "Arm: robot follows you")],
    ["trigger", "Grip"],
    ...(state.recordingEnabled === false ? [] : [
      ["left squeeze", state.recording ? "STOP recording" : "Start recording"],
    ]),
    ["right stick ←→", state.waistFrozen ? "Waist locked by operator" : "Turn waist"],
    ["left stick ↑↓ / ←→", !state.walkingAllowed ? "Walking blocked by operator"
      : !state.locomotion?.enabled ? "Walking disabled"
      : state.locomotion.ready ? "Walk / turn" : "Walk: center stick when armed"],
    ["left stick (hold)", state.ghostShown === false ? "Show robot" : "Hide robot"],
    ["right stick (hold)", "Give robot back"],
    ["Meta button (hold)", "Recenter panels"],
  ];
  c.font = `400 15px ${MONO}`;
  c.fillStyle = BRAND.key;
  c.fillText("CONTROLS", X, 38);
  // Held X is not a control any more (its gain is disabled in the bridge).
  // Tightened to fit rather than grown: the canvas and the panel share one
  // aspect ratio. test_every_legend_row_fits_on_the_canvas holds the bottom.
  const LEGEND = { y0: 65, dy: 23 };
  let y = LEGEND.y0;
  for (const [key, what] of rows) {
    fit(c, key.toUpperCase(), 400, 17, MONO, 232 - X - 12);
    c.fillStyle = BRAND.key;
    c.fillText(key.toUpperCase(), X, y);
    fit(c, what, 500, 21, FONT, W - X - 232);
    c.fillStyle = key === "right stick ←→" && state.waistFrozen ? BRAND.warn : BRAND.cream;
    c.fillText(what, 232, y);
    y += LEGEND.dy;
  }
}
