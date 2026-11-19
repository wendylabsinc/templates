"""The legend, the camera and what "home" means, from the operator's request.

  "add in the VR the buttons and the live feed of the camera so i can know what
   to press and what the robot is seeing, make sure that when i press home it
   goes to home and all the joints go there, and while going to home it does
   not take input from my hands"

The driving context: they are inside a headset. They cannot see the laptop,
and coming out to look ENDS the XR session, which drops the socket. So
anything they need while driving has to be in there with them.
"""
import asyncio
import pathlib
import re
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))
sys.path.insert(0, str(ROOT / "onboard"))
import server                                                    # noqa: E402

JS = (ROOT / "teleop" / "web" / "teleop.js").read_text()
# The status panel's drawing (legend, state, brand) lives in web/hud.js.
HUD = (ROOT / "teleop" / "web" / "hud.js").read_text()


# ---- home ------------------------------------------------------------------

class _Sink:
    def __init__(self): self.cmds = []
    def command(self, n): self.cmds.append(n); return True


def test_home_holds_first_so_it_works_while_armed():
    """go_home refuses while ARMED -- "homing while armed is two drivers" --
    so a press mid-attempt used to do nothing at all, which is not what a
    button labelled home should do."""
    sink = _Sink()
    server.send_home(sink)
    assert sink.cmds == ["hold", "home"], sink.cmds


def test_the_hold_comes_before_the_home():
    """Order is the whole point: home after hold works, home before hold is
    refused."""
    sink = _Sink()
    server.send_home(sink)
    assert sink.cmds.index("hold") < sink.cmds.index("home")


def test_hand_input_cannot_reach_the_arms_while_homing():
    """Both independent publishers explicitly refuse while homing.

    Two things commanding the same joints is what go_home exists to refuse
    being half of; splitting Dex3 off the arm path must not weaken that gate.
    """
    import g1_onboard as ob
    import inspect
    arm = inspect.getsource(ob.Commander.apply_arm)
    hands = inspect.getsource(ob.Commander.apply_hands)
    assert 'self.last_refusal = "homing"' in arm
    assert 'self.last_hand_refusal = "homing"' in hands
    for body in (arm, hands):
        assert 'getattr(self, "homing", False)' in body, \
            "read defensively: a Commander built by __new__ in a test has no .homing"


def test_home_still_takes_the_hands_with_it():
    """From #28, and the operator asked for 'all the joints'. A home that
    leaves the robot holding a fist is not a known starting pose."""
    src = (ROOT / "onboard" / "g1_onboard.py").read_text()
    body = src.split("    def go_home(")[1].split("\n    def ")[0]
    assert "ctrl_dual_hand" in body


# ---- the camera endpoint ----------------------------------------------------

def _req(app):
    return types.SimpleNamespace(app=app, match_info={}, query={}, method="GET",
                                 headers={})


def test_the_newest_camera_frame_is_served_as_a_jpeg():
    cam = types.SimpleNamespace(_latest=({"seq": 7, "frame_id": 99}, b"\xff\xd8\xffDATA", 0.0))
    r = asyncio.run(server.camera_frame_handler(_req({"camera": cam})))
    assert r.content_type == "image/jpeg"
    assert r.body == b"\xff\xd8\xffDATA"
    assert r.headers["X-Frame-Seq"] == "7"


def test_it_is_never_cached():
    """A cached frame is a picture of the past presented as the present."""
    cam = types.SimpleNamespace(_latest=({"seq": 1}, b"\xff\xd8\xff", 0.0))
    r = asyncio.run(server.camera_frame_handler(_req({"camera": cam})))
    assert "no-store" in r.headers["Cache-Control"]


def test_no_camera_is_404_rather_than_an_empty_image():
    for app in ({"camera": None}, {"camera": types.SimpleNamespace(_latest=None)}, {}):
        r = asyncio.run(server.camera_frame_handler(_req(app)))
        assert r.status == 404


# ---- the legend -------------------------------------------------------------

def test_every_control_the_operator_has_is_in_the_legend():
    """If a button is not on it, the operator has to leave VR to ask -- and
    leaving VR ends the session."""
    legend = HUD.split("const rows = [")[1].split("];")[0]
    for control in ("BOTH sticks", "trigger", "left squeeze", "right stick ←→"):
        assert control in legend, f"{control} is not in the legend"
    # Voice prompts were removed on request; attempt labels long before.
    # Held X went too: its fore/aft gain is disabled in the bridge.
    for removed in ("right squeeze", "A / B", "thank you", "drink", "X (hold)", "B / Y", "success", "fail", "mark an attempt"):
        assert removed not in legend


def test_the_legend_says_which_way_the_record_toggle_will_go():
    """'start recording' and 'STOP recording' are different instructions, and
    the operator cannot see the laptop to find out which applies."""
    assert "state.recording ?" in HUD


def test_the_overlay_is_drawn_every_frame_and_the_canvas_is_not():
    """The canvas is redrawn only when something on it changed: this is in the
    frame loop, and a 512x256 text redraw at 90 Hz is not free."""
    assert "drawOverlayView(glRef, view, vp, hudMatrix);" in JS
    assert "if (hudDirty)" in JS


PANEL_RE = r"panelMvp\(view, head, ([-\d.]+), HUD_Y(?: ([+-]) ([\d.]+))?, HUD_DIST, ([\d.]+), ([\d.]+)\)"


def _panel_calls():
    """(x, dy, hw, hh) of every in-page panel, offsets signed."""
    out = []
    for x, sign, dy, hw, hh in re.findall(PANEL_RE, JS):
        d = float(dy or 0) * (-1 if sign == "-" else 1)
        out.append((float(x), d, float(hw), float(hh)))
    return out


def test_the_panels_do_not_overlap_each_other():
    """The camera sits in the MIDDLE now, by request -- it is what the robot
    sees and what the operator works from. The legend has to clear its edge or
    one is drawn over the other; the health panel above the legend has to
    clear the legend's top. Checked as boxes, in metres at HUD_DIST.

    Two panels now: the camera and the one status panel. The health panel
    went at the operator's request -- its alerts are lines on the status
    panel, and the rest is on the laptop."""
    calls = _panel_calls()
    # Three now: the camera, and the status and controls stacked beside it.
    assert len(calls) == 3, calls
    boxes = [(x - hw, x + hw, dy - hh, dy + hh) for x, dy, hw, hh in calls]
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            apart_x = a[1] <= b[0] or b[1] <= a[0]
            apart_y = a[3] <= b[2] or b[3] <= a[2]
            assert apart_x or apart_y, f"panels overlap: {a} {b}"


def test_the_camera_leaves_the_periphery_clear():
    """Centred is not the same as covering everything: the operator is still
    looking at the real robot around it."""
    x, hw, hh = re.search(
        r"fpvGL\.texCam,\s*\n\s*panelMvp\(view, head, ([-\d.]+), HUD_Y[^,]*, HUD_DIST, ([\d.]+), ([\d.]+)\)",
        JS).groups()
    import math
    d = float(re.search(r'get\("huddist"\).*?: ([\d.]+);', JS, re.S).group(1))
    half_angle = math.degrees(math.atan(_default_scale() * float(hw) / d))
    assert half_angle < 25, f"the camera spans {2*half_angle:.0f} deg of view"


def test_the_panels_are_placed_in_the_world_not_painted_on_the_screen():
    """The bug the operator felt: "very close to my eyes... not well aligned
    for 2 eyes".

    A quad written straight into clip space lands at the same screen position
    in both eyes. Zero disparity is the image of something infinitely far,
    handed to eyes converged an arm's length away -- so it reads as doubled
    and at the wrong distance. Placed at a real distance, each eye is given
    its own view and the disparity is correct by construction.
    """
    assert "view.projectionMatrix" in JS and "view.transform.inverse.matrix" in JS
    assert "viewerPose.transform.matrix" in JS
    assert "const hudMatrix = HUD_LOCK ? hudAnchor : viewerPose.transform.matrix;" in JS
    overlay = JS.split("function drawOverlay")[1].split("\nfunction ")[0]
    assert "drawQuad(" not in overlay, "the screen-space path is the bug"


def test_the_panel_distance_is_past_arms_reach_and_not_out_of_reach():
    """Much closer and the eyes have to converge hard on something they are
    only glancing at; much further and the legend's text is too small to
    read at the canvas's 512 px. At 1.4 the operator said "too close to my
    eyes and i dont see fully the edge of the left panels": 2.2 now."""
    d = float(re.search(r'get\("huddist"\).*?: ([\d.]+);', JS, re.S).group(1))
    assert 2.0 <= d <= 2.5, f"{d} m"


def _default_scale():
    return float(re.search(r'get\("hudscale"\).*?: ([\d.]+);', JS, re.S).group(1))


def test_the_left_panels_edge_is_a_short_head_turn_away():
    """The status panel is the outermost thing drawn. Head-locked, its edge
    had to sit inside the lenses' sharp field (under 30 deg: at 1.4 m it was
    39 and the operator could not see it). Fixed in the room and made bigger
    on request, it is read by turning the head a little, so the bound is a
    comfortable glance -- under 40 deg -- at the DEFAULT scale, not at the
    literals."""
    import math
    d = float(re.search(r'get\("huddist"\).*?: ([\d.]+);', JS, re.S).group(1))
    k = _default_scale()
    outer = k * max(abs(x) + hw for x, _, hw, _ in _panel_calls())
    assert math.degrees(math.atan(outer / d)) < 40, f"outer edge {math.degrees(math.atan(outer / d)):.0f} deg off axis"
    assert "const HUD_LOCK" in JS, "the relaxed bound assumes the panels stay in the room"


def test_the_panels_stay_in_the_room_and_recenter_on_request():
    """Anchored once, re-anchored only by Quest's recenter, and the same
    anchor for panels and miniatures so they stay together."""
    assert "if (HUD_LOCK && !hudAnchor) hudAnchor = levelAnchor(head);" in JS
    assert 'refSpace.addEventListener("reset", () => { hudAnchor = null; hudResets++; });' in JS
    assert 'get("hudlock") !== "0"' in JS, "head-locked stays one URL knob away"
    assert "[view], { getViewport: () => vp }, hudMatrix, GHOST, HUD_DIST," in JS
    legend = HUD.split("const rows = [")[1].split("];")[0]
    assert "Recenter panels" in legend


def test_one_knob_scales_the_whole_layout_toward_the_centre():
    """?hudscale= shrinks every panel's offset AND size, and the miniatures
    with them, from inside the headset. Applied in panelMvp so the layout
    literals the other tests read stay at scale 1."""
    assert 'get("hudscale")' in JS
    assert "Math.max(0.4, Math.min(2.0, v))" in JS, "clamp, never revert"
    assert ": 1.1;" in JS.split('get("hudscale")')[1][:200], "1.35 was too big once steady"
    body = JS.split("function panelMvp")[1].split("\n}")[0]
    for term in ("hw * k", "hh * k", "x * k", "y * k"):
        assert term in body, term
    assert "ghost.scaleParams(ghost.ghostParams(location.search), HUD_SCALE)" in JS


def test_the_matrix_multiply_is_column_major():
    """WebGL takes column-major, and a transposed MVP puts the panel
    somewhere that is not wrong enough to look obviously broken."""
    import numpy as np
    src = JS.split("function mat4mul(a, b) {")[1].split("\n}")[0]
    # Re-implement the indexing the JS uses and check it against numpy.
    rng = np.random.default_rng(0)
    A = rng.random((4, 4)); B = rng.random((4, 4))
    a = A.flatten(order="F"); b = B.flatten(order="F")
    o = [0.0] * 16
    for c in range(4):
        for r in range(4):
            o[c * 4 + r] = (a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1]
                            + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3])
    assert np.allclose(np.array(o).reshape(4, 4, order="F"), A @ B)


def test_the_panel_height_can_be_tuned_from_the_url():
    """How low is comfortable is judged while wearing the headset, and a
    redeploy to nudge a panel costs the session -- it drops the socket and the
    operator has to enter VR again."""
    assert 'get("hudy")' in JS
    # Clamped, not reverted: ?hudy=-2 falling back to the default looks
    # identical to no parameter at all, and the operator who tried -2 and -5
    # concluded the knob did nothing.
    assert "Math.max(-1.5, Math.min(1.5, v))" in JS, "clamp, never revert"
    assert "return -0.15;" in JS, "a non-numeric value still needs a default"
    assert "METRES" in JS, "the units changed with the 3D placement; say so"


def test_the_polls_stop_when_the_session_ends():
    """Two timers per session, and sessions end often -- a headset taken off
    ends one. Leaking them would poll the bridge forever."""
    assert "clearInterval(hudPoll)" in JS and "clearTimeout(camPoll)" in JS


def test_camera_refresh_is_self_paced_at_sensor_rate():
    """Never overlap image requests, but do not cap a 15 FPS sensor at 4 FPS."""
    assert "const CAM_INTERVAL_MS = 1000 / 15" in JS
    camera_loop = JS.split("const CAM_INTERVAL_MS = 1000 / 15", 1)[1].split(
        "let sessionEnded", 1)[0]
    assert "setTimeout(pollCamera" in camera_loop
    assert "setInterval" not in camera_loop


# ---- arming from inside the headset -----------------------------------------

class _ArmSink:
    def __init__(self, state="HOLDING"):
        self.onboard_state = state
        self.cmds = []
    def command(self, n): self.cmds.append(n); return True


def _sticks(left=True, right=True):
    return {"hands": [
        {"hand": "left", "source": "controller", "buttons": {"stick": left}},
        {"hand": "right", "source": "controller", "buttons": {"stick": right}}]}


def test_both_sticks_start_the_ready_sequence():
    """Every session tonight was armed from the laptop, and reaching for the
    laptop means taking the headset off -- which ends the XR session and drops
    the socket. The one action that starts a session could only be done by
    breaking it."""
    app = {"sink": _ArmSink("HOLDING"), "retargeter": None}
    coro = server.arm_on_sticks(_sticks(), {}, app)
    assert coro is not None
    asyncio.run(coro)
    assert "arm" in app["sink"].cmds


def test_one_stick_alone_does_nothing():
    """This is the control that makes the robot follow your hands. It wants
    two hands and a decision, not a nudge."""
    for l, r in ((True, False), (False, True)):
        app = {"sink": _ArmSink(), "retargeter": None}
        assert server.arm_on_sticks(_sticks(l, r), {}, app) is None
        assert app["sink"].cmds == []


def test_holding_them_is_one_press():
    st, app = {}, {"sink": _ArmSink(), "retargeter": None}
    first = server.arm_on_sticks(_sticks(), st, app)
    assert first is not None
    asyncio.run(first)
    for _ in range(10):
        assert server.arm_on_sticks(_sticks(), st, app) is None


def test_second_stick_press_holds_then_homes_when_armed():
    """Stopping must be at least as easy as starting, and the same reach."""
    app = {"sink": _ArmSink("ARMED"), "retargeter": None}
    assert server.arm_on_sticks(_sticks(), {}, app) is None
    assert app["sink"].cmds == ["hold", "home"]


def test_stick_toggle_arms_then_homes_after_release_and_second_press():
    sink = _ArmSink("HOLDING")
    app, stats = {"sink": sink}, {}
    asyncio.run(server.arm_on_sticks(_sticks(), stats, app))
    sink.onboard_state = "ARMED"
    assert server.arm_on_sticks(_sticks(), stats, app) is None
    assert sink.cmds == ["arm"]
    server.arm_on_sticks(_sticks(False, False), stats, app)
    assert server.arm_on_sticks(_sticks(), stats, app) is None
    assert sink.cmds == ["arm", "hold", "home"]


def test_home_stick_press_is_edge_triggered():
    sink = _ArmSink("ARMED")
    app, stats = {"sink": sink}, {}
    for _ in range(20):
        assert server.arm_on_sticks(_sticks(), stats, app) is None
    assert sink.cmds == ["hold", "home"]


def test_stick_press_during_home_cannot_rearm_or_arm_on_release():
    sink = _ArmSink("HOLDING")
    sink.onboard_homing = True
    app, stats = {"sink": sink}, {}
    result = server.arm_on_sticks(_sticks(), stats, app)
    if result is not None:
        result.close()
    assert result is None
    sink.onboard_homing = False
    assert server.arm_on_sticks(_sticks(), stats, app) is None
    assert sink.cmds == []
    server.arm_on_sticks(_sticks(False, False), stats, app)
    asyncio.run(server.arm_on_sticks(_sticks(), stats, app))
    assert sink.cmds == ["arm"]


def test_it_takes_control_first_from_standby():
    app = {"sink": _ArmSink("STANDBY"), "retargeter": None}
    coro = server.arm_on_sticks(_sticks(), {}, app)
    asyncio.run(coro)
    assert app["sink"].cmds[0] == "take_control"


def test_it_zeroes_the_wrists_before_arming():
    """The wrist joints are solved RELATIVE to the zero pose. Arming without
    one makes the first commanded wrist whatever the last zero happened to be."""
    zeroed = []
    class _R:
        def zero_wrists(self): zeroed.append(True); return "ok"
    app = {"sink": _ArmSink("HOLDING"), "retargeter": _R()}
    asyncio.run(server.arm_on_sticks(_sticks(), {}, app))
    assert zeroed, "armed without zeroing"


def test_it_prepares_controller_grips_before_arming():
    prepared = []
    class _R:
        def prepare_controller_grips(self, measured): prepared.append(measured)
    sink = _ArmSink("HOLDING")
    sink.last_hand_q = {"left": [0.2] * 7, "right": [0.3] * 7}
    app = {"sink": sink, "retargeter": _R()}
    asyncio.run(server.arm_on_sticks(_sticks(), {}, app))
    assert prepared == [sink.last_hand_q]
    assert sink.cmds[-1] == "arm"


def test_hand_tracked_frames_never_arm():
    app = {"sink": _ArmSink(), "retargeter": None}
    f = {"hands": [{"hand": "left", "source": "hand", "buttons": {"stick": True}},
                   {"hand": "right", "source": "hand", "buttons": {"stick": True}}]}
    assert server.arm_on_sticks(f, {}, app) is None


def test_the_legend_shows_it_first():
    """A control nobody can find is a control nobody has."""
    legend = HUD.split("const rows = [")[1].split("];")[0]
    assert "BOTH sticks" in legend
    assert legend.index("BOTH sticks") < legend.index("trigger")


# ---- what the panel can actually read ---------------------------------------

def test_the_status_says_whether_a_session_is_recording():
    """The legend says 'start recording' or 'STOP recording' from this. There
    was no such field, so it always said start -- telling an operator who
    cannot see the laptop to begin a session already running."""
    src = (ROOT / "teleop" / "bridge" / "server.py").read_text()
    handler = src.split("async def control_status_handler")[1].split("\nasync def ")[0]
    assert '"session"' in handler and "recording" in handler


# ---- the panels must fit what they show -------------------------------------

def _num(pattern, text=None):
    return float(re.search(pattern, text or JS, re.S).group(1))


def test_every_legend_row_fits_on_the_canvas():
    """Every control remains visible as new rows are added to the HUD."""
    rows = HUD.split("const rows = [")[1].split("];")[0].count("[\"")
    height = _num(r"HUD_W = \d+, HUD_H = (\d+);", HUD)
    # TWO layouts now: the list starts lower and tightens when a verdict is
    # drawn above it (state.outcome). Both must fit, so both are checked --
    # the version that only reads the first number would pass while the
    # verdict layout ran off the bottom, and the row it cut would be the one
    # the operator had not learned yet.
    layouts = [(int(a), int(b)) for a, b in
               re.findall(r"y0:\s*(\d+),\s*dy:\s*(\d+)", HUD)]
    assert layouts, "could not read the legend layouts (LEGEND = ... y0/dy)"
    for start, step in layouts:
        last = start + (rows - 1) * step
        assert last <= height - 8, (
            f"{rows} rows from y={start} step {step} reach {last} on a canvas "
            f"{height} tall; the last is cut off")


def test_the_side_panels_match_their_canvas_aspect():
    """A stretched legend reads as a blurry one, and the operator would report
    it as a focus problem rather than a geometry one. Status and controls are
    separate panels now; each matches its own canvas, in-page and as layers."""
    for tex, dims, layer in (("texHud", r"STATUS_W = (\d+), STATUS_H = (\d+);", "make(t.status, 0.32, 0.18, STATUS_W, STATUS_H)"),
                             ("texKeys", r"HUD_W = (\d+), HUD_H = (\d+);", "make(t.keys, 0.32, 0.165, HUD_W, HUD_H)")):
        w, h = (float(v) for v in re.search(dims, HUD).groups())
        hw, hh = re.search(
            r"fpvGL\." + tex + r",\s*\n\s*panelMvp\(view, head, [-\d.]+, HUD_Y[^,]*, HUD_DIST, ([\d.]+), ([\d.]+)\)",
            JS).groups()
        assert abs((float(hw) / float(hh)) - (w / h)) < 0.01, f"{tex}: panel {float(hw)/float(hh):.3f} vs canvas {w/h:.3f}"
        assert layer in JS


def test_camera_grows_without_changing_directions_or_center():
    """Enlarge only the feed; preserve the side panels and clear margins."""
    calls = _panel_calls()
    cam = next(c for c in calls if c[0] == 0.0)
    side = sorted((c for c in calls if c[0] != 0.0), key=lambda c: -c[1])
    status, keys = side
    assert cam[2:] == pytest.approx((0.44, 0.33))
    assert all(panel[0] + panel[2] < -cam[2] for panel in side)
    stack_top = status[1] + status[3]
    stack_bottom = keys[1] - keys[3]
    assert (stack_top + stack_bottom) / 2 == pytest.approx(cam[1])
    assert status[2:] == pytest.approx((0.32, 0.18))
    assert keys[2:] == pytest.approx((0.32, 0.165))
    gap = (status[1] - status[3]) - (keys[1] + keys[3])
    assert 0.02 <= gap <= 0.05, f"gap {gap:.3f} m"


def test_the_camera_panel_is_four_by_three_like_the_sensor():
    """640x480. A panel of any other ratio either squashes the image or crops
    it, and then what the operator watches is not what the robot sent."""
    hw, hh = re.search(
        r"fpvGL\.texCam,\s*\n\s*panelMvp\(view, head, [-\d.]+, HUD_Y[^,]*, HUD_DIST, ([\d.]+), ([\d.]+)\)",
        JS).groups()
    assert abs((float(hw) / float(hh)) - (640 / 480)) < 0.01, \
        f"camera panel is {float(hw)/float(hh):.3f}, sensor is {640/480:.3f}"


def test_the_whole_image_is_drawn_rather_than_a_crop():
    """uv spans the full quad, so every pixel the robot sent is on screen."""
    assert "uv = (pos + 1.0) * 0.5" in JS
    assert "CLAMP_TO_EDGE" in JS, "a 640x480 texture is not power-of-two"


def test_the_panel_is_opaque_enough_to_read_over_passthrough():
    """It is blended over a LIT ROOM. Every percent of transparency is room
    light washing the text out, and the operator reported it as faded."""
    body = HUD.split("export function drawStatusPanel")[1]
    alpha = float(re.search(r"rgba\(\d+,\d+,\d+,([\d.]+)\)", body).group(1))
    assert alpha >= 0.9, f"panel is {alpha} opaque over passthrough"


def test_both_legend_columns_are_bright():
    """Grey on a translucent dark panel is grey on grey once the room shows
    through it."""
    legend = HUD.split("for (const [key, what] of rows) {")[1].split("\n  }")[0]
    used = set(re.findall(r"BRAND\.(\w+)", legend))
    assert used, "the legend's colours come from BRAND"
    for name in used:
        hexv = re.search(name + r': "#([0-9a-fA-F]{6})"', HUD).group(1)
        lum = max(int(hexv[i:i+2], 16) for i in (0, 2, 4))
        assert lum >= 0x9f, f"BRAND.{name} #{hexv} is too dim to read over passthrough"


# ---- what the status panel says, from /control/status ------------------------

def test_the_panel_says_armed_offline_and_homing_and_keeps_the_alerts():
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    # A plain string with one substitution, not an f-string: `wendy init`
    # renders .py files as Go templates, so no double opening brace here.
    script = """
    import('HUD_URI').then((h) => {
      const v = (d) => h.stateView(h.hudFromStatus(d));
      console.log(JSON.stringify({
        armed: v({state: "ARMED — teleop motion enabled", connected: true}),
        holding: v({state: "HOLDING", connected: true}),
        offline: v({state: "ARMED", connected: false}),
        homing: v({state: "HOLDING", connected: true, homing: true}),
        alerts: h.hudFromStatus({hand_error: true,
                                  motor_heat_alert: {active: true, joint: "left_elbow_joint", temperature_c: 81}}).alerts,
        waist: h.hudFromStatus({waist_frozen: true}).waistFrozen,
      }));
    }).catch((e) => { console.error(e); process.exit(1); });
    """.replace("HUD_URI", (ROOT / "teleop" / "web" / "hud.js").as_uri())
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["armed"]["word"] == "ARMED" and out["armed"]["tone"] == "armed"
    assert out["holding"]["tone"] == "idle" and "both sticks" in out["holding"]["line"]
    assert out["offline"]["word"] == "OFFLINE", "a lost robot must not still read ARMED"
    assert out["homing"]["word"] == "RETURNING HOME"
    lines = out["alerts"].split("\n")
    assert len(lines) == 2, "at most two alert lines fit above the controls"
    assert "Motor hot: left elbow 81" in lines[0]
    assert lines[1].startswith("Hand motor error")
    assert out["waist"] is True


def test_room_fixed_panels_go_to_the_compositor_with_a_fallback():
    """Placed by the compositor at each refresh, not with the browser's
    23-47 ms prediction -- and the page's own drawing whenever layers are
    missing or refused, so the panels never disappear."""
    assert '["hand-tracking", "layers"]' in JS
    assert 'get("hudlayers") !== "0"' in JS
    assert "createQuadLayer({" in JS and "getSubImage(layer, frame)" in JS
    assert "session.updateRenderState({ layers: [hudLayers.proj, cam, status, keys] });" in JS
    # Once layers are in use the page's image must be a projection layer,
    # or the white/orange robots drawn into it are never shown.
    assert 'binding.createProjectionLayer({ textureType: "texture" })' in JS
    assert "getViewSubImage(hudLayers.proj, view)" in JS
    assert "projLayer = new XRWebGLLayer(session, gl, { alpha: true });\n    session.updateRenderState({ baseLayer: projLayer });" in JS
    assert "if (!fpvGL || (hudLayers && hudLayers.cam)) return;" in JS, "never drawn twice"
    assert "session.renderState.baseLayer" not in JS, "null once layers are in use"
    # Same places and sizes as the in-page panels.
    assert "panelPose(anchor, 0.0, HUD_Y + 0.12, HUD_DIST, HUD_SCALE)" in JS
    assert "panelPose(anchor, -0.82, HUD_Y + 0.30, HUD_DIST, HUD_SCALE)" in JS
    assert "panelPose(anchor, -0.82, HUD_Y - 0.075, HUD_DIST, HUD_SCALE)" in JS
    assert "make(t.cam, 0.44, 0.33, 640, 480)" in JS
    # The heartbeat says which is running, and how often the anchor reset.
    assert 'hud_mode: hudLayers && hudLayers.cam ? "layers" : "page"' in JS
    src = (ROOT / "teleop" / "bridge" / "server.py").read_text()
    assert '"hud_mode", "hud_resets"' in src


def test_the_page_code_is_revalidated_on_every_load():
    """A redeploy must reach the headset on the next reload."""
    import types as _t
    resp = _t.SimpleNamespace(headers={})
    for path in ("/teleop.js", "/hud.js", "/index.html", "/g1_chain.json"):
        resp.headers = {}
        asyncio.run(server._no_stale_web_code(_t.SimpleNamespace(method="GET", path=path), resp))
        assert resp.headers.get("Cache-Control") == "no-cache", path
    resp.headers = {"Cache-Control": "no-store"}
    asyncio.run(server._no_stale_web_code(_t.SimpleNamespace(method="GET", path="/camera/frame.jpg"), resp))
    assert resp.headers["Cache-Control"] == "no-store", "existing policies are left alone"
    src = (ROOT / "teleop" / "bridge" / "server.py").read_text()
    assert "app.on_response_prepare.append(_no_stale_web_code)" in src
