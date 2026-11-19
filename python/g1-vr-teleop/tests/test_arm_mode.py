"""One switch for the arm math: TELEOP_ARM=solver|geometry|fingers.

Two arm paths exist. `solver` is Unitree's vendored Pinocchio/CasADi IK, all
fourteen joints, one solve per frame off the frame loop. `geometry` is the
closed-form path built here: elbow from reach, shoulder from direction (yaw
only when roll cannot reach), wrist from orientation against the forearm.
Until now picking one meant knowing five environment variables and the deploy
script hard-coded the geometry. A baseline comparison against the vendored
solver -- same headset, same bridge, same onboard, only the arm math differs
-- needs the choice to be one word, visible in the banner, in /control/status
and in every recording's header, so an episode can never be misread as the
other path's.
"""
import json
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))
import server  # noqa: E402
from tests.test_endpoint_gating import _app, _req  # noqa: E402

HERE = os.path.dirname(__file__)


# ---- the switch -------------------------------------------------------------

def test_solver_is_the_vendored_ik_with_the_worker_on():
    cfg = server.arm_mode_from_env({"TELEOP_ARM": "solver"})
    assert cfg["mode"] == "solver"
    assert cfg["forearm"] is False and cfg["fingers_only"] is False
    assert cfg["async_ik"] is True, "inline, the solve sets the rate for the fingers too"


def test_solver_can_still_run_the_ik_inline_when_asked():
    cfg = server.arm_mode_from_env({"TELEOP_ARM": "solver", "TELEOP_ASYNC_IK": "0"})
    assert cfg["async_ik"] is False


def test_geometry_is_the_closed_form_with_todays_defaults():
    cfg = server.arm_mode_from_env({"TELEOP_ARM": "geometry"})
    assert cfg["mode"] == "geometry"
    assert cfg["forearm"] is True and cfg["shoulder"] is True
    assert cfg["shoulder_yaw"] is True and cfg["wrist_fk"] is True
    assert cfg["async_ik"] is False, "the geometry path never touches the IK"


def test_geometry_honours_the_individual_flags():
    cfg = server.arm_mode_from_env({"TELEOP_ARM": "geometry", "TELEOP_SHOULDER": "0"})
    assert cfg["shoulder"] is False
    cfg = server.arm_mode_from_env({"TELEOP_ARM": "geometry", "TELEOP_SHOULDER_YAW": "0",
                                    "TELEOP_WRIST_FK": "0"})
    assert cfg["shoulder"] is True and cfg["shoulder_yaw"] is False and cfg["wrist_fk"] is False


def test_fingers_only_is_a_mode_too():
    cfg = server.arm_mode_from_env({"TELEOP_ARM": "fingers"})
    assert cfg["mode"] == "fingers" and cfg["fingers_only"] is True and cfg["forearm"] is False


def test_an_unknown_mode_is_refused_loudly():
    with pytest.raises(ValueError):
        server.arm_mode_from_env({"TELEOP_ARM": "magic"})


def test_without_the_switch_the_old_flags_still_decide():
    """Every deploy so far used TELEOP_FOREARM / TELEOP_SHOULDER / TELEOP_FINGERS_ONLY.
    They keep working, and are reported under the mode name they amount to."""
    assert server.arm_mode_from_env({"TELEOP_FOREARM": "1"})["mode"] == "geometry"
    assert server.arm_mode_from_env({"TELEOP_FINGERS_ONLY": "1"})["mode"] == "fingers"
    legacy = server.arm_mode_from_env({})
    assert legacy["mode"] == "solver" and legacy["async_ik"] is False, \
        "no flags at all was always the inline IK; that must not change underneath anyone"
    assert server.arm_mode_from_env({"TELEOP_ASYNC_IK": "1"})["async_ik"] is True


def test_the_switch_wins_over_a_stray_old_flag():
    """A deploy that says solver must not become geometry because an old
    TELEOP_FOREARM=1 was left in the environment."""
    cfg = server.arm_mode_from_env({"TELEOP_ARM": "solver", "TELEOP_FOREARM": "1"})
    assert cfg["mode"] == "solver" and cfg["forearm"] is False


def test_apply_arm_mode_sets_exactly_the_retargeter_flags():
    class R:
        forearm = shoulder = shoulder_yaw = wrist_fk = fingers_only = None
    r = R()
    server.apply_arm_mode(r, server.arm_mode_from_env({"TELEOP_ARM": "geometry"}))
    assert (r.forearm, r.shoulder, r.shoulder_yaw, r.wrist_fk, r.fingers_only) == (True, True, True, True, False)
    r = R()
    server.apply_arm_mode(r, server.arm_mode_from_env({"TELEOP_ARM": "solver"}))
    assert (r.forearm, r.fingers_only) == (False, False)


def test_the_worker_flag_is_prepared_before_the_retargeter_is_built():
    """G1Retargeter reads TELEOP_ASYNC_IK in its constructor, so the switch
    has to write it into the environment before make_retargeter runs."""
    env = {"TELEOP_ARM": "solver"}
    cfg = server.arm_mode_from_env(env)
    server.prepare_env_for_arm_mode(cfg, env)
    assert env["TELEOP_ASYNC_IK"] == "1"
    env = {"TELEOP_ARM": "geometry", "TELEOP_ASYNC_IK": "1"}
    server.prepare_env_for_arm_mode(server.arm_mode_from_env(env), env)
    assert env["TELEOP_ASYNC_IK"] == "0", "the geometry path must never start the IK worker"


# ---- where the choice is visible ----------------------------------------------

def test_control_status_reports_the_arm_mode():
    app = _app()
    app["arm_mode"] = "solver"
    resp = asyncio_run(server.control_status_handler(_req(app, {})))
    assert json.loads(resp.text)["arm_mode"] == "solver"


def test_the_session_header_records_the_arm_mode(tmp_path):
    from session_recorder import SessionRecorder
    rec = SessionRecorder(str(tmp_path))
    sid = rec.start("x", meta={"arm_mode": "solver"})["session"]
    rec.stop()
    header = json.loads(open(tmp_path / f"{sid}.jsonl").readline())
    assert header["type"] == "header" and header["arm_mode"] == "solver"


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)
