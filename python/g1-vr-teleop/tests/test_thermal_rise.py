"""A fast-rising joint temperature is visible before it reaches the hot limit."""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "teleop", "bridge"))

from thermal_rise import ThermalRiseMonitor  # noqa: E402
from tests.test_sink_ack import FakeWs, bare_sink  # noqa: E402


def _ramp(monitor, joint, rate, start=40.0):
    for t in range(9):
        monitor.observe({joint: start + rate * t}, now=float(t))


def test_sustained_fast_rise_names_the_joint_and_rate():
    monitor = ThermalRiseMonitor()
    _ramp(monitor, "right_hand_middle_0_joint", 0.30)
    status = monitor.status(now=8.0)
    assert status["fastest"]["joint"] == "right_hand_middle_0_joint"
    assert status["fastest"]["temperature_c"] == pytest.approx(42.4)
    assert status["fastest"]["rise_c_per_s"] == pytest.approx(0.30)
    assert status["fastest"]["level"] == "warn"


def test_critical_rise_is_red_but_flat_cooling_and_short_spikes_do_not_warn():
    hot = ThermalRiseMonitor()
    _ramp(hot, "left_elbow_joint", 0.60)
    assert hot.status(now=8.0)["fastest"]["level"] == "bad"

    flat = ThermalRiseMonitor()
    for t, value in enumerate((40, 40, 40, 39, 39, 39, 39, 39, 39)):
        flat.observe({"left_elbow_joint": value}, now=float(t))
    assert flat.status(now=8.0)["fastest"] is None

    short = ThermalRiseMonitor()
    short.observe({"left_elbow_joint": 40}, now=0.0)
    short.observe({"left_elbow_joint": 44}, now=1.0)
    assert short.status(now=1.0)["fastest"] is None


def test_sink_combines_named_hand_and_body_joint_temperatures():
    sink = bare_sink()
    sink.last_hand_q = None
    msg = {
        "ack": 1,
        "state": "ARMED",
        "applied": True,
        "body": {"idx": [12, 15, 25], "q": [0.0] * 3, "dq": [0.0] * 3,
                 "temperature": [43, 44, 46]},
        "hand_state": {
            "left": None,
            "right": {"temperature": [50, 51, 52, 53, 54, 55, 56]},
        },
    }
    asyncio.run(sink._read(FakeWs([msg])))
    readings = sink._joint_temperatures()
    assert readings["waist_yaw_joint"] == 43
    assert readings["left_shoulder_pitch_joint"] == 44
    assert readings["right_elbow_joint"] == 46
    assert readings["right_hand_index_1_joint"] == 56
    thermal = sink.status()["thermal"]
    assert thermal["monitored_joints"] == 10
    assert thermal["fastest"] is None, "one sample cannot establish a trend"
