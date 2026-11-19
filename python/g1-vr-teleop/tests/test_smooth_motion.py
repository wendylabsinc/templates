"""Smoothness: no teleport out of a tracking gap, and a real low-pass.

Both exist because the operator reported the arms "fighting to move" and the
first full-stack episode showed why: right_shoulder_pitch took a 142 deg step
in ONE frame against a p50 step of 0.11 deg. _degap held the jump for two
frames and then handed over the whole thing at once.
"""
import math
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "teleop" / "bridge"))
import retarget_g1                                            # noqa: E402

R = retarget_g1.G1Retargeter


def _bare():
    """A retargeter without __init__ -- the heavy imports are not the subject."""
    r = R.__new__(R)
    r._wrist_prev = None
    r._gap_runs = None
    r.n_wrist_gaps = 0
    r.n_wrist_jumps = 0
    return r


class TestDegapConverges:
    def test_a_persisted_jump_is_not_handed_over_whole(self):
        """The bug: two frames held, then the entire gap in one step."""
        r = _bare()
        limit = R.MAX_STEP["shoulder_pitch"]
        assert r._degap("right", "shoulder_pitch", 0.0) == 0.0
        target = 2.48                                  # ~142 deg away
        seen = [r._degap("right", "shoulder_pitch", target) for _ in range(4)]
        steps = [abs(b - a) for a, b in zip([0.0] + seen, seen)]
        assert max(steps) <= limit + 1e-9, \
            f"a single step of {max(steps):.3f} rad got through (limit {limit})"

    def test_it_still_holds_a_one_frame_glitch(self):
        """Converging must not cost the glitch rejection it replaced."""
        r = _bare()
        r._degap("right", "shoulder_pitch", 0.30)
        assert r._degap("right", "shoulder_pitch", 2.48) == 0.30
        assert r._degap("right", "shoulder_pitch", 0.31) == 0.31   # glitch passed

    def test_it_eventually_reaches_a_target_that_persists(self):
        """Holding forever was the older bug; it must still yield."""
        r = _bare()
        r._degap("left", "elbow", 0.0)
        v = None
        for _ in range(200):
            v = r._degap("left", "elbow", 1.5)
        assert v == pytest.approx(1.5, abs=1e-6)

    def test_small_moves_are_untouched(self):
        r = _bare()
        r._degap("left", "elbow", 0.0)
        assert r._degap("left", "elbow", 0.05) == 0.05


class TestSmoothing:
    def _r(self, tau):
        """The FIXED filter specifically -- one-euro is the default now, and
        these cases are about what a constant time constant does."""
        r = R.__new__(R)
        r._smooth = None
        r._smooth_t = None
        r._euro = None
        r.frozen = frozenset()
        r.smooth_tau = 0.0
        r.smooth_mode, r.smooth_min_cutoff, r.smooth_beta = "ema", 1.0, 1.0
        r.set_smooth_tau(tau)
        return r

    def test_off_by_default_is_a_passthrough(self):
        r = self._r(0.0)
        out = {"left_elbow_joint": 1.0}
        assert r._smoothed(out)["left_elbow_joint"] == 1.0

    def test_a_step_is_spread_over_time_not_taken_at_once(self):
        r = self._r(0.2)
        r._smoothed({"left_elbow_joint": 0.0})        # seed
        r._smooth_t -= 0.011                          # ~one 90 fps frame
        v = r._smoothed({"left_elbow_joint": 1.0})["left_elbow_joint"]
        assert 0.0 < v < 0.10, f"one frame moved {v:.3f} of a unit step"

    def test_fingers_are_never_smoothed(self):
        """A late grasp feels broken in a way a late elbow does not."""
        r = self._r(0.5)
        k = retarget_g1.DEX3_RIGHT[0]
        r._smoothed({k: 0.0})
        r._smooth_t -= 0.05
        assert r._smoothed({k: 1.0})[k] == 1.0

    def test_a_longer_tau_moves_less_in_the_same_time(self):
        def after_one_frame(tau):
            r = self._r(tau)
            r._smoothed({"left_elbow_joint": 0.0})
            r._smooth_t -= 0.011
            return r._smoothed({"left_elbow_joint": 1.0})["left_elbow_joint"]
        assert after_one_frame(0.4) < after_one_frame(0.1)

    def test_dt_is_measured_so_the_filter_keeps_its_character(self):
        """Same elapsed time must give the same result at any frame rate."""
        def over(n_frames):
            r = self._r(0.2)
            r._smoothed({"left_elbow_joint": 0.0})
            for _ in range(n_frames):
                r._smooth_t -= 0.09 / n_frames
                v = r._smoothed({"left_elbow_joint": 1.0})["left_elbow_joint"]
            return v
        assert over(9) == pytest.approx(over(3), abs=0.02)

    def test_a_long_dropout_cannot_carry_most_of_the_way(self):
        r = self._r(0.2)
        r._smoothed({"left_elbow_joint": 0.0})
        r._smooth_t -= 30.0                            # a 30 s gap
        v = r._smoothed({"left_elbow_joint": 1.0})["left_elbow_joint"]
        assert v < 1.0, "the dt cap did not hold"

    def test_it_refuses_nonsense(self):
        r = self._r(0.0)
        for bad in (-1.0, float("nan"), float("inf")):
            with pytest.raises(ValueError):
                r.set_smooth_tau(bad)

    def test_it_is_capped_at_one_second(self):
        assert self._r(0.0).set_smooth_tau(99.0) == 1.0


class TestFreeze:
    """A frozen joint must hold the pose it was armed at, and only that."""

    def _r(self):
        r = R.__new__(R)
        r._smooth, r._smooth_t, r._euro, r.smooth_tau = None, None, None, 0.0
        r.frozen = frozenset()
        r.smooth_mode, r.smooth_min_cutoff, r.smooth_beta = "ema", 1.0, 1.0
        return r

    def test_a_frozen_wrist_is_pinned_to_its_zero_pose_value(self):
        r = self._r()
        r.set_frozen("right_wrist_pitch_joint")
        out = r._smoothed({"right_wrist_pitch_joint": 0.9, "right_wrist_roll_joint": 0.9})
        assert out["right_wrist_pitch_joint"] == 0.0
        assert out["right_wrist_roll_joint"] == 0.9, "only the named joint freezes"

    def test_clearing_restores_it(self):
        r = self._r()
        r.set_frozen("right_wrist_pitch_joint")
        assert r.set_frozen("") == []
        assert r._smoothed({"right_wrist_pitch_joint": 0.9})["right_wrist_pitch_joint"] == 0.9

    def test_it_refuses_a_name_that_is_not_an_arm_joint(self):
        r = self._r()
        for bad in ("right_hand_thumb_1_joint", "nonsense"):
            with pytest.raises(ValueError):
                r.set_frozen(bad)

    def test_several_at_once(self):
        r = self._r()
        got = r.set_frozen("right_wrist_pitch_joint,left_wrist_pitch_joint")
        assert got == ["left_wrist_pitch_joint", "right_wrist_pitch_joint"]

    def test_freezing_survives_smoothing(self):
        """Order matters: freeze first, then filter, or the filter drags the
        frozen joint toward a target it is meant to be ignoring."""
        r = self._r()
        r.set_smooth_tau(0.2)
        r.set_frozen("right_wrist_pitch_joint")
        r._smoothed({"right_wrist_pitch_joint": 0.0})
        r._smooth_t -= 0.05
        assert r._smoothed({"right_wrist_pitch_joint": 1.0})["right_wrist_pitch_joint"] == 0.0


class TestOneEuro:
    """Smoother AND less laggy: a fixed filter cannot do both, this can."""

    def _r(self, **kw):
        r = R.__new__(R)
        r._smooth, r._smooth_t, r._euro, r.smooth_tau = None, None, None, 0.0
        r.frozen = frozenset()
        r.smooth_mode, r.smooth_min_cutoff, r.smooth_beta = "oneeuro", 1.0, 1.0
        if kw:
            r.set_smooth(**kw)
        return r

    def _run(self, r, series, dt=0.011):
        out = []
        for v in series:
            r._smooth_t = (r._smooth_t or __import__("time").monotonic()) - dt
            out.append(r._smoothed({"left_elbow_joint": v})["left_elbow_joint"])
        return out

    def test_a_still_hand_is_smoothed_hard(self):
        """Jitter about a fixed pose is what you SEE, so damp it."""
        import random
        random.seed(7)
        noisy = [0.5 + random.uniform(-0.02, 0.02) for _ in range(200)]
        r = self._r()
        out = self._run(r, noisy)[50:]
        spread_in = max(noisy[50:]) - min(noisy[50:])
        spread_out = max(out) - min(out)
        assert spread_out < spread_in * 0.5, f"{spread_out:.4f} vs {spread_in:.4f}"

    def test_a_fast_move_is_not_held_back_the_way_a_fixed_filter_holds_it(self):
        """Lag is what you FEEL, so get out of the way when moving."""
        ramp = [0.02 * i for i in range(60)]
        euro = self._run(self._r(), ramp)
        ema = self._r(mode="ema")
        ema.set_smooth_tau(0.25)
        fixed = self._run(ema, ramp)
        assert euro[-1] > fixed[-1], (
            f"one-euro trailed the fixed filter: {euro[-1]:.3f} vs {fixed[-1]:.3f}")
        assert abs(euro[-1] - ramp[-1]) < abs(fixed[-1] - ramp[-1]) * 0.5

    def test_beta_zero_is_a_plain_fixed_lowpass(self):
        """With no speed term the cutoff is constant -- the degenerate case."""
        r = self._r(beta=0.0)
        out = self._run(r, [1.0] * 40)
        assert out[-1] == pytest.approx(1.0, abs=0.02)

    def test_fingers_are_still_never_filtered(self):
        r = self._r()
        k = retarget_g1.DEX3_RIGHT[0]
        r._smoothed({k: 0.0})
        r._smooth_t -= 0.05
        assert r._smoothed({k: 1.0})[k] == 1.0

    def test_off_is_a_passthrough(self):
        r = self._r(mode="off")
        assert self._run(r, [0.0, 1.0])[-1] == 1.0

    def test_it_refuses_nonsense(self):
        r = self._r()
        with pytest.raises(ValueError):
            r.set_smooth(mode="banana")
        for bad in (-1.0, 0.0, float("nan"), 1e9):
            with pytest.raises(ValueError):
                r.set_smooth(min_cutoff=bad)

    def test_a_frozen_joint_stays_frozen_under_one_euro(self):
        r = self._r()
        r.set_frozen("left_wrist_pitch_joint")
        r._smoothed({"left_wrist_pitch_joint": 0.0})
        r._smooth_t -= 0.05
        assert r._smoothed({"left_wrist_pitch_joint": 1.0})["left_wrist_pitch_joint"] == 0.0

    def test_joint_motion_cost_makes_elbow_heavier_and_shoulder_lighter(self):
        """A cost is an output preference, not a switch to the slow IK solver."""
        r = self._r()
        r.set_smooth_cost(shoulder=0.75, elbow=1.5, wrist=1.0)
        zeros = {"left_shoulder_pitch_joint": 0.0,
                 "left_elbow_joint": 0.0,
                 "left_wrist_pitch_joint": 0.0}
        r._smoothed(dict(zeros))
        r._smooth_t -= 0.011
        r._smoothed(dict(zeros))
        r._smooth_t -= 0.011
        out = r._smoothed({k: 1.0 for k in zeros})
        assert out["left_shoulder_pitch_joint"] > out["left_wrist_pitch_joint"]
        assert out["left_elbow_joint"] < out["left_wrist_pitch_joint"]

    def test_joint_motion_cost_is_symmetric_and_validated(self):
        r = self._r()
        assert r.set_smooth_cost(shoulder=0.75, elbow=1.5, wrist=1.0) == {
            "shoulder": 0.75, "elbow": 1.5, "wrist": 1.0,
        }
        r._smoothed({"left_elbow_joint": 0.0, "right_elbow_joint": 0.0})
        r._smooth_t -= 0.011
        out = r._smoothed({"left_elbow_joint": 1.0, "right_elbow_joint": 1.0})
        assert out["left_elbow_joint"] == pytest.approx(out["right_elbow_joint"])
        with pytest.raises(ValueError):
            r.set_smooth_cost(elbow=0.0)


class TestBridgeSmoothingConfig:
    """The filter existed, but no deployed bridge ever enabled it.

    Keep the library/replay default unchanged and make the live deployment opt
    in explicitly.  That way recorded raw targets and offline tools do not
    silently change character, while every normal Thor deploy gets the filter.
    """

    def test_no_environment_keeps_the_library_path_off(self):
        import server
        assert server.smoothing_from_env({}) == {
            "mode": "off", "min_cutoff": 1.0, "beta": 1.0, "tau": 0.0,
            "joint_cost": {"shoulder": 1.0, "elbow": 1.0, "wrist": 1.0},
        }

    def test_environment_is_applied_to_the_retargeter(self):
        import server
        r = self._r()
        cfg = server.smoothing_from_env({
            "TELEOP_SMOOTH_MODE": "oneeuro",
            "TELEOP_SMOOTH_MIN_CUTOFF": "1.25",
            "TELEOP_SMOOTH_BETA": "2.5",
            "TELEOP_SMOOTH_SHOULDER_COST": "0.75",
            "TELEOP_SMOOTH_ELBOW_COST": "1.5",
        })
        server.apply_smoothing(r, cfg)
        assert r.set_smooth() == {
            "mode": "oneeuro", "min_cutoff": 1.25, "beta": 2.5, "tau": 0.0,
        }
        assert r.smooth_joint_cost == {"shoulder": 0.75, "elbow": 1.5, "wrist": 1.0}

    def test_fixed_ema_tau_is_applied_too(self):
        import server
        r = self._r()
        cfg = server.smoothing_from_env({
            "TELEOP_SMOOTH_MODE": "ema", "TELEOP_SMOOTH_TAU": "0.12",
        })
        server.apply_smoothing(r, cfg)
        assert r.smooth_mode == "ema" and r.smooth_tau == pytest.approx(0.12)

    def test_invalid_startup_configuration_fails_loudly(self):
        import server
        with pytest.raises(ValueError):
            server.smoothing_from_env({"TELEOP_SMOOTH_MODE": "banana"})
        with pytest.raises(ValueError):
            server.smoothing_from_env({"TELEOP_SMOOTH_BETA": "not-a-number"})

    def test_control_status_exposes_the_active_filter(self):
        import asyncio
        import json
        import server
        from tests.test_endpoint_gating import _app, _req
        app = _app()
        app["retargeter"] = self._r()
        server.apply_smoothing(app["retargeter"], {
            "mode": "oneeuro", "min_cutoff": 1.0, "beta": 3.0, "tau": 0.0,
            "joint_cost": {"shoulder": 0.75, "elbow": 1.5, "wrist": 1.0},
        })
        status = json.loads(asyncio.run(server.control_status_handler(_req(app, {}))).text)
        assert status["smoothing"] == {
            "mode": "oneeuro", "min_cutoff": 1.0, "beta": 3.0, "tau": 0.0,
            "joint_cost": {"shoulder": 0.75, "elbow": 1.5, "wrist": 1.0},
        }

    @staticmethod
    def _r():
        r = R.__new__(R)
        r._smooth, r._smooth_t, r._euro, r.smooth_tau = None, None, None, 0.0
        r.frozen = frozenset()
        r.smooth_mode, r.smooth_min_cutoff, r.smooth_beta = "off", 1.0, 1.0
        return r


class TestRunConfigInHeader:
    """A run worth keeping is a run worth reproducing.

    The header used to carry arm_mode alone, and answering "what was the
    configuration of the good run?" meant reading back an assistant's command
    history. None of this is recoverable from the frames: gains and frozen
    joints leave no trace in a target, and smoothing leaves one nobody can
    invert.
    """

    def _app(self):
        import types
        r = R.__new__(R)
        r.shoulder = r.wrist_fk = r.forearm = True
        r.fingers_only = False
        r.shoulder_yaw = False
        r.smooth_mode, r.smooth_min_cutoff, r.smooth_beta, r.smooth_tau = "oneeuro", 1.0, 1.5, 0.0
        r.smooth_joint_cost = {"shoulder": 0.75, "elbow": 1.5, "wrist": 1.0}
        r.frozen = frozenset({"right_wrist_pitch_joint"})
        r._cal = {"a": (0, 1)}
        return {"retargeter": r,
                "sink": types.SimpleNamespace(onboard_state="ARMED",
                                              onboard_gains={"right_elbow_joint": {"kp": 110.0}}),
                "source": "xr", "arm_mode": "geometry"}

    def test_it_captures_what_shapes_the_targets(self):
        import server
        c = server._run_config(self._app())
        assert c["arm_mode"] == "geometry" and c["source"] == "xr"
        assert c["shoulder"] is True and c["shoulder_yaw"] is False
        assert c["smoothing"]["mode"] == "oneeuro" and c["smoothing"]["beta"] == 1.5
        assert c["smoothing"]["joint_cost"]["elbow"] == 1.5
        assert c["frozen"] == ["right_wrist_pitch_joint"]
        assert c["gains"]["right_elbow_joint"]["kp"] == 110.0
        assert c["finger_cal_joints"] == 1

    def test_the_gains_the_onboard_last_reported_are_recorded(self):
        import server
        app = self._app()
        app["sink"].onboard_gains = {"right_elbow_joint": {"kp": 125.0, "kd": 2.5}}
        c = server._run_config(app)
        assert c["gains"]["right_elbow_joint"] == {"kp": 125.0, "kd": 2.5}

    def test_the_calibrations_that_live_in_code_are_captured_too(self):
        """They survive deploys, so they are not in the runtime settings --
        but a recording is read months later against a changed constant."""
        import server
        c = server._run_config(self._app())
        assert "right" in c["elbow_cal"] and "right" in c["operator_shoulder"]
        assert "roll" in c["human_wrist"]

    def test_a_broken_field_costs_the_field_not_the_recording(self):
        import server
        app = self._app()

        class _Broken:
            onboard_state = "ARMED"

            @property
            def onboard_gains(self):
                raise RuntimeError("broken")
        app["sink"] = _Broken()
        c = server._run_config(app)
        assert c["arm_mode"] == "geometry", "one bad field must not lose the rest"


class TestGripThumb0:
    """thumb_0 was pinned neutral on both input paths.

    Measured with the operator on controllers: every other Dex3 joint used
    100 % of its range and this one used 0 %, while the same joint used 100 %
    on the hand path -- so the controller grip was strictly poorer than the
    fingers it replaced, and the operator felt it as a palm that would not
    open or close fully.
    """

    def teardown_method(self):
        retarget_g1.set_grip_thumb0(0.0)

    def test_zero_is_exactly_the_old_behaviour(self):
        retarget_g1.set_grip_thumb0(0.0)
        for f in (0.0, 0.5, 1.0):
            assert retarget_g1.grip_q("right", f)[0] == 0.0
            assert retarget_g1.grip_q("left", f)[0] == 0.0

    def test_it_moves_both_ways_and_reaches_the_limit(self):
        for v, want in ((1.0, 1.047), (-1.0, -1.047)):
            retarget_g1.set_grip_thumb0(v)
            got = retarget_g1.grip_q("right", 1.0)[0]
            assert abs(got - want) < 0.01, f"thumb0={v}: {got:+.3f}"

    def test_open_is_still_neutral_whatever_the_setting(self):
        """A hand told to open must open, not sit half-rotated."""
        for v in (-1.0, -0.5, 0.0, 0.5, 1.0):
            retarget_g1.set_grip_thumb0(v)
            assert retarget_g1.grip_q("right", 0.0)[0] == 0.0

    def test_the_other_six_joints_are_untouched(self):
        base = retarget_g1.grip_q("right", 1.0)[1:]
        retarget_g1.set_grip_thumb0(1.0)
        assert retarget_g1.grip_q("right", 1.0)[1:] == base

    def test_both_hands_move_together(self):
        retarget_g1.set_grip_thumb0(1.0)
        l = retarget_g1.grip_q("left", 1.0)[0]
        r = retarget_g1.grip_q("right", 1.0)[0]
        assert abs(l) == pytest.approx(abs(r), abs=1e-9)

    def test_it_refuses_out_of_range(self):
        for bad in (1.5, -2.0, float("nan"), float("inf")):
            with pytest.raises(ValueError):
                retarget_g1.set_grip_thumb0(bad)


class TestLiveFps:
    """A dead stream must not read as a healthy one."""

    def test_a_healthy_stream_reads_as_before(self):
        import server, time as _t
        st = {"fps": 89.7, "frames": 45, "since": _t.monotonic() - 0.5}
        assert server._live_fps(st) == 89.7

    def test_a_stalled_stream_decays_toward_zero(self):
        import server, time as _t
        st = {"fps": 89.7, "frames": 0, "since": _t.monotonic() - 30.0}
        assert server._live_fps(st) == 0.0

    def test_it_decays_gradually_rather_than_cliffing(self):
        import server, time as _t
        a = server._live_fps({"fps": 90.0, "frames": 90, "since": _t.monotonic() - 3.0})
        b = server._live_fps({"fps": 90.0, "frames": 90, "since": _t.monotonic() - 10.0})
        assert 0 < b < a < 90.0

    def test_no_window_yet_is_not_an_error(self):
        import server
        assert server._live_fps({}) == 0.0
        assert server._live_fps({"fps": 12.0}) == 12.0


class TestWristScale:
    """Sensitivity, not range. The operator reported wrist PITCH reaching
    unnatural positions too easily; a tighter clamp would leave it just as
    twitchy until it hit a wall."""

    def _r(self):
        r = R.__new__(R)
        r._smooth, r._smooth_t, r._euro, r.smooth_tau = None, None, None, 0.0
        r.frozen = frozenset()
        r.smooth_mode, r.smooth_min_cutoff, r.smooth_beta = "off", 1.0, 1.0
        r.wrist_scale = {"roll": 1.0, "pitch": 1.0, "yaw": 1.0}
        return r

    def test_one_is_untouched(self):
        r = self._r()
        assert r._smoothed({"right_wrist_pitch_joint": 0.8})["right_wrist_pitch_joint"] == 0.8

    def test_a_half_scale_halves_the_motion(self):
        r = self._r(); r.set_wrist_scale(pitch=0.5)
        assert r._smoothed({"right_wrist_pitch_joint": 0.8})["right_wrist_pitch_joint"] == 0.4

    def test_it_only_touches_the_named_axis(self):
        r = self._r(); r.set_wrist_scale(pitch=0.5)
        out = r._smoothed({"right_wrist_pitch_joint": 0.8, "right_wrist_roll_joint": 0.8,
                           "right_elbow_joint": 0.8})
        assert out["right_wrist_roll_joint"] == 0.8
        assert out["right_elbow_joint"] == 0.8, "the elbow is not a wrist"

    def test_both_hands_scale_together(self):
        r = self._r(); r.set_wrist_scale(yaw=0.25)
        out = r._smoothed({"left_wrist_yaw_joint": 0.8, "right_wrist_yaw_joint": 0.8})
        assert out["left_wrist_yaw_joint"] == out["right_wrist_yaw_joint"] == 0.2

    def test_zero_is_a_freeze(self):
        r = self._r(); r.set_wrist_scale(roll=0.0)
        assert r._smoothed({"right_wrist_roll_joint": 1.2})["right_wrist_roll_joint"] == 0.0

    def test_it_refuses_nonsense(self):
        r = self._r()
        for bad in (-0.1, 1.5, float("nan")):
            with pytest.raises(ValueError):
                r.set_wrist_scale(pitch=bad)
        with pytest.raises(ValueError):
            r.set_wrist_scale(elbow=0.5)

    def test_scaling_happens_before_the_filter(self):
        """Or the filter chases a target that is then scaled out from under it."""
        r = self._r(); r.set_smooth_tau(0.2); r.smooth_mode = "ema"
        r.set_wrist_scale(pitch=0.5)
        r._smoothed({"right_wrist_pitch_joint": 0.0})
        for _ in range(400):
            r._smooth_t -= 0.02
            v = r._smoothed({"right_wrist_pitch_joint": 1.0})["right_wrist_pitch_joint"]
        assert v == pytest.approx(0.5, abs=1e-3), f"settled at {v:.3f}, not the scaled target"
