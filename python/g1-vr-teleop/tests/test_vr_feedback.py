"""What the headset tells the operator besides the picture.

Three silences, each learned the hard way: a leash or a clamp was felt as the
arm lagging; a stalled knuckle was felt as nothing at all; a half-second-old
camera frame looked exactly like now. And the robot's health -- hand
temperature, the right hand's motor fault, whether the camera is up -- lived
on the phone page, unreadable from inside the headset.

web/feedback.js plans haptics, stamps the picture's age and renders a health
panel. This file
runs the real module under node, and checks the bridge sends what it needs.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))
sys.path.insert(0, str(ROOT / "onboard"))
import server                                                    # noqa: E402

JS = ROOT / "teleop" / "web" / "feedback.js"
TELEOP = (ROOT / "teleop" / "web" / "teleop.js").read_text()
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


def run_js(body):
    # Concatenated, not an f-string: `wendy init` renders .py files as Go
    # templates, so this file must never contain a double opening brace.
    script = ("import('" + JS.as_uri() + "').then(async (f) => { " + body + " })\n"
              "  .catch((e) => { console.error(e); process.exit(1); });\n")
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout)


# ---- the bridge sends what the feedback needs ------------------------------------

class _Sink:
    onboard_state = "ARMED"
    onboard_q = [0.0] * 14
    onboard_q_t = time.monotonic()
    last_clamped = ["left_elbow_joint"]
    last_leashed = ["right_wrist_roll_joint"]
    last_refused = None
    last_hand_backoff = {"left": [], "right": ["right_hand_index_1_joint"]}
    def commanded(self): return {}
    def last_hands(self): return ([0.0] * 7, [0.0] * 7)


class _Cam:
    def latest_ref(self): return {"seq": 7, "t": 1.0, "age_s": 0.075}


def test_the_message_carries_limits_and_camera():
    m = server.robot_state_message(_Sink(), {"camera": _Cam()})
    assert m["clamped"] == ["left_elbow_joint"] and m["leashed"] == ["right_wrist_roll_joint"]
    assert m["hand_backoff"]["right"] == ["right_hand_index_1_joint"]
    assert m["cam"]["age_s"] == 0.075


def test_without_an_app_the_message_is_still_whole():
    m = server.robot_state_message(_Sink())
    assert m["cam"] is None
    assert m["clamped"] == ["left_elbow_joint"]


def test_the_handler_hands_the_app_to_the_push():
    body = (ROOT / "teleop" / "bridge" / "server.py").read_text().split("async def ws_handler")[1].split("\nasync def ")[0]
    assert "push_robot_state(ws, sink, app=request.app)" in body


# ---- haptics ------------------------------------------------------------------------

@needs_node
def test_a_clamped_left_joint_buzzes_the_left_hand_only():
    got = run_js("""
      const m = {type: "robot", state: "ARMED", clamped: ["left_elbow_joint"], leashed: [], hand_backoff: null};
      console.log(JSON.stringify(f.hapticPlan(m, 0, 10, null).pulses));
    """)
    assert got == [{"hand": "left", "intensity": 0.7, "ms": 80}]


@needs_node
def test_a_stalled_knuckle_buzzes_its_hand():
    got = run_js("""
      const m = {type: "robot", state: "ARMED", clamped: [], leashed: [],
                 hand_backoff: {left: [], right: ["right_hand_index_1_joint"]}};
      console.log(JSON.stringify(f.hapticPlan(m, 0, 10, null).pulses));
    """)
    assert got == [{"hand": "right", "intensity": 0.5, "ms": 80}]


@needs_node
def test_a_held_cause_buzzes_at_most_every_150_ms_not_every_frame():
    """90 frames a second with a leash held is 90 buzzes a second, which is a
    controller that vibrates continuously and tells the operator nothing."""
    got = run_js("""
      const m = {type: "robot", state: "ARMED", clamped: [], leashed: ["left_elbow_joint"], hand_backoff: null};
      let st = null, n = 0;
      for (let t = 0; t < 1000; t += 11) { const p = f.hapticPlan(m, t, t, st); st = p.state; n += p.pulses.length; }
      console.log(JSON.stringify(n));
    """)
    assert 6 <= got <= 7, got


@needs_node
def test_nothing_buzzes_when_not_armed_or_when_the_message_is_stale():
    got = run_js("""
      const m = {type: "robot", state: "HOLDING", clamped: ["left_elbow_joint"], leashed: [], hand_backoff: null};
      const a = f.hapticPlan(m, 0, 10, null).pulses.length;
      const armed = Object.assign({}, m, {state: "ARMED"});
      const b = f.hapticPlan(armed, 0, 2000, null).pulses.length;    // 2 s old
      const c = f.hapticPlan(null, 0, 10, null).pulses.length;
      console.log(JSON.stringify([a, b, c]));
    """)
    assert got == [0, 0, 0]


def test_the_page_pulses_the_controller_on_the_named_side_and_can_be_silenced():
    assert 'import * as fb from "./feedback.js";' in TELEOP
    assert "fb.hapticPlan(robotMsg, robotRecvMs, performance.now(), hapticState)" in TELEOP
    assert "src.handedness !== p.hand" in TELEOP
    assert "hapticActuators?.[0]" in TELEOP and "act.pulse(p.intensity, p.ms)" in TELEOP
    assert 'get("haptics") !== "0"' in TELEOP


# ---- the picture's age ---------------------------------------------------------------

@needs_node
def test_the_age_turns_amber_past_200_ms_and_red_past_500():
    got = run_js("""
      console.log(JSON.stringify([f.ageLabel(75), f.ageLabel(250), f.ageLabel(800), f.ageLabel(null)]));
    """)
    assert got[0] == {"text": "75 ms", "level": "ok"}
    assert got[1]["level"] == "warn" and got[2]["level"] == "bad"
    assert got[3] == {"text": "no frame", "level": "bad"}


@needs_node
def test_the_age_on_screen_includes_the_time_since_the_message_arrived():
    """The bridge's age is for when the message LEFT; a message that sat for
    300 ms describes a frame 300 ms older than it says."""
    got = run_js("""
      const m = {type: "robot", cam: {seq: 1, t: 0, age_s: 0.075}};
      console.log(JSON.stringify([f.frameAgeMs(m, 1000, 1300), f.frameAgeMs({type: "robot", cam: null}, 0, 0)]));
    """)
    assert got[0] == pytest.approx(375) and got[1] is None


def test_the_camera_panel_is_composited_with_the_age():
    assert "fb.composeCamera(camCtx, img, fb.frameAgeMs(robotMsg, robotRecvMs, now)" in TELEOP
    assert "camImage = camCanvas; camDirty = true;" in TELEOP


# ---- health --------------------------------------------------------------------------------

def test_the_hand_thresholds_are_the_thermal_guards():
    """Amber and red on the panel mean what the onboard's guard means."""
    import dex3_thermal
    src = JS.read_text()
    warm = float(re.search(r"HAND_WARM_C = ([\d.]+)", src).group(1))
    hot = float(re.search(r"HAND_HOT_C = ([\d.]+)", src).group(1))
    assert warm == dex3_thermal.WARM_C and hot == dex3_thermal.HOT_C


@needs_node
def test_health_rows_colour_the_faulted_hot_hand_and_a_missing_camera():
    got = run_js("""
      const d = {connected: true, state: "ARMED", acked: 100, applied: 80, hand_temp_max_c: 60, hand_error: true,
                 thermal: {monitored_joints: 17, fastest: {joint: "left_elbow_joint", temperature_c: 48,
                           rise_c_per_s: 0.31, level: "warn"}},
                 camera: null,
                 xr_connected: true, xr_fps: 88, xr_clients: 2, arm_mode: "geometry", build: {dirty: false}};
      console.log(JSON.stringify(f.healthRows(d)));
    """)
    rows = {r[0]: r for r in got}
    assert rows["robot"][1] == "ARMED · applied 80%" and rows["robot"][2] == "ok"
    assert "MOTOR ERROR" in rows["hands"][1] and rows["hands"][2] == "bad"
    assert rows["heat"] == ["heat", "L elbow +0.31 °C/s · 48 °C", "warn"]
    assert rows["camera"] == ["camera", "none", "bad"]
    assert "2 tabs" in rows["link"][1] and rows["link"][2] == "warn", "a stale tab is worth seeing"
    assert len(got) == 6


@needs_node
def test_a_warm_hand_is_amber_and_a_cool_one_green():
    got = run_js("""
      const base = {connected: true, state: "HOLDING", camera: {connected: true, fps: 8.4, last_age_s: 0.075},
                    xr_connected: false};
      console.log(JSON.stringify([f.healthRows(Object.assign({hand_temp_max_c: 48}, base))[1],
                                  f.healthRows(Object.assign({hand_temp_max_c: 56}, base))[1],
                                  f.healthRows(Object.assign({hand_temp_max_c: 80}, base))[1],
                                  f.healthRows(base)[3]]));
    """)
    assert got[0][2] == "ok" and got[1][2] == "warn" and got[2][2] == "bad"
    assert got[3] == ["camera", "8.4 fps · 75 ms", "ok"]


@needs_node
def test_thermal_row_is_stable_or_unavailable_without_a_fast_rise():
    got = run_js("""
      const base = {connected: true, state: "HOLDING", hand_temp_max_c: 42,
                    camera: {connected: true, fps: 8, last_age_s: 0.1}};
      console.log(JSON.stringify([
        f.healthRows(Object.assign({thermal: {monitored_joints: 31, fastest: null}}, base))[2],
        f.healthRows(base)[2]
      ]));
    """)
    assert got[0] == ["heat", "31 joints stable", "ok"]
    assert got[1] == ["heat", "no joint temperatures", "warn"]


@needs_node
def test_an_eighty_degree_motor_replaces_the_rate_row_with_a_small_red_alert():
    got = run_js("""
      const base = {connected: true, state: "ARMED", hand_temp_max_c: 60,
                    thermal: {monitored_joints: 31, fastest: {joint: "left_elbow_joint",
                              temperature_c: 62, rise_c_per_s: 0.4, level: "warn"}},
                    camera: {connected: true}};
      console.log(JSON.stringify([
        f.healthRows(Object.assign({motor_heat_alert: {active: true, joint: "right_wrist_pitch_joint",
                                                       temperature_c: 80, threshold_c: 80}}, base))[2],
        f.healthRows(Object.assign({motor_heat_alert: {active: false, joint: "right_wrist_pitch_joint",
                                                       temperature_c: 79, threshold_c: 80}}, base))[2]
      ]));
    """)
    assert got[0] == ["MOTOR HOT", "R wrist pitch 80 °C", "bad"]
    assert got[1] == ["heat", "L elbow +0.40 °C/s · 62 °C", "warn"]


def test_phone_ui_has_a_prominent_rapid_temperature_warning():
    control = (ROOT / "teleop" / "web" / "control.html").read_text()
    assert 'id="thermal-rise"' in control
    assert 's.thermal' in control and 'THERMAL RISE' in control


def test_phone_ui_has_an_eighty_degree_motor_alert():
    control = (ROOT / "teleop" / "web" / "control.html").read_text()
    assert 'id="motor-hot"' in control
    assert 's.motor_heat_alert' in control and 'MOTOR HOT' in control


def test_the_headset_has_no_health_panel_but_keeps_its_alerts():
    """The operator asked for the camera, the robots and ARMED with its
    directions -- nothing else. The health panel went; the alerts that must
    still interrupt (a hot motor, a hand fault) are lines on the
    status panel (hud.js hudFromStatus), and the laptop keeps the rest."""
    assert "texHealth" not in TELEOP and "healthRows" not in TELEOP
    hud = (ROOT / "teleop" / "web" / "hud.js").read_text()
    body = hud.split("export function hudFromStatus")[1].split("\n}")[0]
    for alert in ("motor_heat_alert", "d.hand_error"):
        assert alert in body, alert
