"""Regression tests for the measured orange-to-robot latency causes.

The 2026-09-19 physical trace showed three independent problems: bursts were
processed oldest-first, the bridge sent twice as fast as the physical
publisher, and the arming offset could move an otherwise valid IK target past
this robot's calibrated joint envelope.  These tests keep the fixes at the
wire boundary where the real failure occurred.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))
sys.path.insert(0, str(ROOT / "onboard"))

import g1_onboard as onboard  # noqa: E402
import retarget_g1 as rg  # noqa: E402
import sink_realg1 as realg1  # noqa: E402


def _sink(*, q=None):
    sink = realg1.RealG1Sink.__new__(realg1.RealG1Sink)
    sink._latest = None
    sink._latest_t = 0.0
    sink._lock = threading.Lock()
    sink._last_hands = None
    sink.n_retargeted = 0
    sink.onboard_state = "ARMED"
    sink.onboard_q = list(q or [0.0] * 14)
    sink._arm_offset = None
    sink._warned_no_q = False
    return sink


def _full_targets(value=0.0):
    targets = {name: value for name in rg.ARM_LEFT + rg.ARM_RIGHT}
    targets.update({name: 0.0 for name in rg.DEX3_LEFT + rg.DEX3_RIGHT})
    return targets


def test_a_buffered_burst_keeps_only_the_newest_target():
    async def run():
        mailbox = onboard.LatestTargetMailbox()
        mailbox.put({"seq": 1, "arm": [0.1] * 14})
        mailbox.put({"seq": 2, "arm": [0.2] * 14})
        mailbox.put({"seq": 3, "arm": [0.3] * 14})
        return await asyncio.wait_for(mailbox.get(), timeout=0.1)

    superseded, latest = asyncio.run(run())
    assert [frame["seq"] for frame in superseded] == [1, 2]
    assert latest["seq"] == 3


def test_websocket_burst_applies_the_newest_target_not_every_stale_one():
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    class Commander:
        def __init__(self):
            self.lock = threading.Lock()
            self.state = "ARMED"
            self.measured_arm_q = [0.0] * 14
            self.measured_body = None
            self.hand = None
            self.last_refusal = None
            self.last_rx_t = 0.0
            self.n_rx = self.n_rejected = self.n_superseded = 0
            self.applied = []

        def claim(self):
            return "owner"

        def release(self, token):
            return False

        def operator_disconnected(self, token):
            return "not the controlling connection"

        def reset_rx_age(self):
            return None

        def note_rx_age(self, age):
            return None

        def apply(self, arm, left, right, **kw):
            # Long enough for the reader to collect the rest of the local
            # burst while this runs in the target worker thread.
            time.sleep(0.02)
            self.applied.append(list(arm))
            return True

    async def run():
        cmd = Commander()
        app = web.Application()
        app["cmd"] = cmd
        app["secret"] = ""
        app.router.add_get("/ctrl", onboard.ws_handler)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/ctrl") as ws:
                for seq in range(1, 11):
                    arm = [0.0] * 14
                    arm[0] = seq / 10.0
                    await ws.send_json({"seq": seq, "arm": arm,
                                        "hand_left": [0.0] * 7,
                                        "hand_right": [0.0] * 7})
                acks = []
                while len(acks) < 10:
                    payload = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
                    if "ack" in payload:
                        acks.append(payload)
                    for seq in payload.get("superseded_acks", []):
                        acks.append({"ack": seq, "applied": False,
                                     "refused": "superseded"})
        return cmd, acks

    cmd, acks = asyncio.run(run())
    assert cmd.applied[-1][0] == pytest.approx(1.0)
    assert len(cmd.applied) < 10
    assert sum(ack.get("refused") == "superseded" for ack in acks) == 10 - len(cmd.applied)
    assert [ack["ack"] for ack in acks] == sorted(ack["ack"] for ack in acks)


def test_slow_dex3_write_does_not_suppress_sixty_hz_arm_targets():
    """The 33 ms Dex3 write must not occupy the arm/waist worker.

    This models the measured hardware timing: bridge frames arrive every
    16.7 ms while one hand publish takes roughly 33 ms.  The arm path should
    apply every paced frame; the hand path may independently keep only its
    newest target.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    class Commander:
        def __init__(self):
            self.lock = threading.Lock()
            self.state = "ARMED"
            self.measured_arm_q = [0.0] * 14
            self.measured_body = None
            self.hand = object()
            self.hands_only = False
            self.last_refusal = None
            self.last_rx_t = 0.0
            self.n_rx = self.n_rejected = self.n_superseded = 0
            self.applied_arm = []
            self.applied_hands = []

        def claim(self):
            return "owner"

        def release(self, token):
            return False

        def operator_disconnected(self, token):
            return "not the controlling connection"

        def reset_rx_age(self):
            return None

        def note_rx_age(self, age):
            return None

        def apply_arm(self, arm, *, waist=None, waist_clamped=False):
            self.applied_arm.append(list(arm))
            return True

        def apply_hands(self, left, right, *, seq=None):
            time.sleep(0.033)
            self.applied_hands.append((seq, list(left), list(right)))
            return True

        def apply(self, arm, left, right, **kw):
            # The pre-fix websocket calls this combined path, reproducing the
            # physical serialization of a sub-millisecond arm write behind a
            # 33 ms hand write.
            applied = self.apply_arm(
                arm, waist=kw.get("waist"),
                waist_clamped=kw.get("waist_clamped", False),
            )
            self.apply_hands(left, right, seq=kw.get("seq"))
            return applied

    async def run():
        cmd = Commander()
        app = web.Application()
        app["cmd"] = cmd
        app["secret"] = ""
        app.router.add_get("/ctrl", onboard.ws_handler)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/ctrl") as ws:
                count = 24
                for seq in range(1, count + 1):
                    arm = [0.0] * 14
                    arm[0] = seq / 100.0
                    await ws.send_json({"seq": seq, "arm": arm,
                                        "hand_left": [seq / 100.0] * 7,
                                        "hand_right": [seq / 100.0] * 7})
                    await asyncio.sleep(1.0 / 60.0)
                acks = []
                while len(acks) < count:
                    payload = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
                    if "ack" in payload:
                        acks.append(payload)
                # Give the independent hand worker time to consume the newest
                # target before the websocket's worker tasks are cancelled.
                deadline = time.monotonic() + 0.5
                while (not cmd.applied_hands
                       or cmd.applied_hands[-1][0] != count):
                    if time.monotonic() >= deadline:
                        break
                    await asyncio.sleep(0.005)
        return cmd, acks

    cmd, acks = asyncio.run(run())
    assert len(cmd.applied_arm) == 24
    assert not [ack for ack in acks if ack.get("refused") == "superseded"]
    assert cmd.applied_hands[-1][0] == 24
    assert len(cmd.applied_hands) < len(cmd.applied_arm)


def test_waist_worker_is_not_blocked_by_a_slow_arm_apply():
    """A waist-only target must be acknowledged while the arm worker is busy."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    class Commander:
        def __init__(self):
            self.lock = threading.Lock()
            self.state = "ARMED"
            self.measured_arm_q = [0.0] * 14
            self.measured_body = None
            self.hand = object()
            self.hands_only = False
            self.last_refusal = None
            self.last_rx_t = 0.0
            self.n_rx = self.n_rejected = self.n_superseded = 0
            self.arm_started = threading.Event()
            self.release_arm = threading.Event()
            self.waists = []
            self.last_waist = None

        def claim(self):
            return "owner"

        def release(self, token):
            return False

        def operator_disconnected(self, token):
            return "not the controlling connection"

        def reset_rx_age(self):
            return None

        def note_rx_age(self, age):
            return None

        def apply_arm(self, arm, *, waist=None, waist_clamped=False):
            self.arm_started.set()
            self.release_arm.wait(timeout=1.0)
            return True

        def apply_hands(self, left, right, *, seq=None):
            return True

        def apply_waist(self, target, *, waist_clamped=False, seq=None):
            self.waists.append(target)
            self.last_waist = {"cmd": target, "q": target,
                               "clamped": waist_clamped, "leashed": False}
            return True

    async def run():
        cmd = Commander()
        app = web.Application()
        app["cmd"] = cmd
        app["secret"] = ""
        app.router.add_get("/ctrl", onboard.ws_handler)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/ctrl") as ws:
                await ws.send_json({"seq": 1, "arm": [0.0] * 14,
                                    "hand_left": [0.0] * 7,
                                    "hand_right": [0.0] * 7,
                                    "waist": 0.1, "waist_detached": True})
                assert await asyncio.to_thread(cmd.arm_started.wait, 0.2)
                await ws.send_json({"waist_only": True, "waist_seq": 1,
                                    "waist": 0.25, "t_sent": time.time()})
                receipt = None
                try:
                    deadline = asyncio.get_running_loop().time() + 0.25
                    while asyncio.get_running_loop().time() < deadline:
                        payload = await asyncio.wait_for(ws.receive_json(), timeout=0.25)
                        if "waist_ack" in payload:
                            receipt = payload
                            break
                finally:
                    cmd.release_arm.set()
                return cmd, receipt

    cmd, receipt = asyncio.run(run())
    assert receipt is not None
    assert receipt["waist_ack"] == 1 and receipt["applied"] is True
    assert cmd.waists == pytest.approx([0.25])


def test_velocity_waist_frame_uses_the_independent_worker():
    target = onboard._prepare_waist_target_frame(
        type("Commander", (), {"n_rejected": 0})(),
        {"waist_velocity_only": True, "waist_velocity": -0.5, "waist_seq": 9})
    epoch, received = target.pop("admission")
    assert epoch == 0 and received > 0
    assert target == {"seq": 9, "mode": "velocity", "velocity": -0.5,
                      "velocity_clamped": False}

    class Commander:
        state = "ARMED"
        last_waist = {"cmd": 0.1, "q": 0.09, "velocity": -0.5,
                      "clamped": False, "leashed": False, "stale": False}
        last_waist_refusal = None

        def apply_waist_velocity(self, velocity, *, velocity_clamped=False, seq=None):
            self.seen = (velocity, velocity_clamped, seq)
            return True

    cmd = Commander()
    receipt = onboard._apply_waist_target_frame(cmd, target, [])
    assert cmd.seen == (-0.5, False, 9)
    assert receipt["waist_ack"] == 9 and receipt["applied"] is True


def test_bridge_clock_stays_anchored_and_skips_missed_slots():
    assert realg1._next_deadline(1.000, .010, 1.003) == pytest.approx(1.010)
    assert realg1._next_deadline(1.000, .010, 1.037) == pytest.approx(1.040)


def test_post_offset_ik_target_is_projected_before_it_reaches_the_wire():
    q = [0.0] * 14
    elbow = realg1.RealG1Sink._ARM_INDEX["right_elbow_joint"]
    q[elbow] = 0.2
    sink = _sink(q=q)

    # Capture an offset from a valid neutral retargeter pose.
    first = _full_targets()
    first["right_elbow_joint"] = 1.0
    asyncio.run(sink.apply(first))
    assert sink._latest["arm"][elbow] == pytest.approx(0.2)

    # The raw IK target is inside the datasheet range, but subtracting the
    # arming offset moves the wire command below this robot's calibrated limit.
    second = _full_targets()
    second["right_elbow_joint"] = rg.ARM_JOINT_LIMITS["right_elbow_joint"][0]
    asyncio.run(sink.apply(second))

    lo, _ = rg.ROBOT_LIMITS["right_elbow_joint"]
    assert sink._latest["arm"][elbow] == pytest.approx(lo)
    event = sink.latest_ik_rejection()
    rejected = event["rejected"]["right_elbow_joint"]
    assert rejected["requested_rad"] < lo
    assert rejected["feasible_rad"] == pytest.approx(lo)
    assert rejected["reason"] == "calibrated_joint_limit"
    assert event["raw_rad"]["right_elbow_joint"] == pytest.approx(
        rejected["requested_rad"]
    )
    assert event["feasible_rad"]["right_elbow_joint"] == pytest.approx(lo)


def test_rejected_targets_are_available_as_an_ordered_replay_stream():
    sink = _sink()
    sink._record_ik_rejection(
        raw={"right_elbow_joint": -1.4},
        feasible={"right_elbow_joint": -0.9},
        rejected={
            "right_elbow_joint": {
                "requested_rad": -1.4,
                "feasible_rad": -0.9,
                "limit_rad": [-0.9, 2.0],
                "reason": "calibrated_joint_limit",
            }
        },
    )
    sink._record_ik_rejection(
        raw={"right_elbow_joint": -1.5},
        feasible={"right_elbow_joint": -0.9},
        rejected={
            "right_elbow_joint": {
                "requested_rad": -1.5,
                "feasible_rad": -0.9,
                "limit_rad": [-0.9, 2.0],
                "reason": "calibrated_joint_limit",
            }
        },
    )
    events = sink.ik_rejection_events()
    assert [event["v"] for event in events] == [1, 2]
    assert [event["raw_rad"]["right_elbow_joint"] for event in events] == [-1.4, -1.5]
