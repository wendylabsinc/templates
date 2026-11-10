"""get_mode_machine must not race the subscribe thread for the DDS queue.

The vendored constructor did `self.lowstate_subscriber.Read().mode_machine`.
ChannelSubscriber.Read() hands out each queued sample ONCE, and the
controller's own subscribe thread is draining that queue every 2 ms. So the
constructor raced its own thread for the next sample and got None whenever the
thread won -- an AttributeError at startup that came and went with timing.

The fake subscriber below models exactly that contract: one sample queued,
Read() returns it once and None thereafter. The old code fails on it every
time; the real robot only failed some of the time, which is worse.
"""
import importlib
import os
import pathlib
import re
import sys
import types

import pytest

REAL = pathlib.Path(__file__).parent.parent / "onboard"
# conftest imports retarget_g1 and expects a test module to have put bridge/ on the path.
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "teleop" / "bridge"))


def _lowstate(mode_machine):
    motors = [types.SimpleNamespace(q=0.0, dq=0.0) for _ in range(35)]
    return types.SimpleNamespace(motor_state=motors, mode_machine=mode_machine)


def _lowcmd():
    motors = [types.SimpleNamespace(mode=0, q=0.0, dq=0.0, tau=0.0, kp=0.0, kd=0.0)
              for _ in range(35)]
    return types.SimpleNamespace(motor_cmd=motors, mode_pr=0, mode_machine=0, crc=0)


class _Subscriber:
    """Read() yields each queued sample once. Whoever calls first gets it."""
    MODE_MACHINE = 5

    def __init__(self, topic, typ):
        self._queue = [_lowstate(self.MODE_MACHINE)]

    def Init(self):
        pass

    def Read(self):
        return self._queue.pop(0) if self._queue else None


class _Publisher:
    def __init__(self, topic, typ):
        self.writes = 0

    def Init(self):
        pass

    def Write(self, msg):
        self.writes += 1


class _CRC:
    def Crc(self, msg):
        return 0


@pytest.fixture
def g1_arm(monkeypatch):
    """Import real/g1_arm.py against a fake unitree_sdk2py."""
    def mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, name, m)
        return m

    mod("unitree_sdk2py")
    mod("unitree_sdk2py.core")
    mod("unitree_sdk2py.core.channel", ChannelPublisher=_Publisher,
        ChannelSubscriber=_Subscriber, ChannelFactoryInitialize=lambda *a, **k: None)
    mod("unitree_sdk2py.idl")
    mod("unitree_sdk2py.idl.unitree_hg")
    mod("unitree_sdk2py.idl.unitree_hg.msg")
    mod("unitree_sdk2py.idl.unitree_hg.msg.dds_", LowCmd_=object, LowState_=object)
    mod("unitree_sdk2py.idl.unitree_go")
    mod("unitree_sdk2py.idl.unitree_go.msg")
    mod("unitree_sdk2py.idl.unitree_go.msg.dds_", LowCmd_=object, LowState_=object)
    mod("unitree_sdk2py.idl.default", unitree_hg_msg_dds__LowCmd_=_lowcmd,
        unitree_go_msg_dds__LowCmd_=_lowcmd)
    mod("unitree_sdk2py.utils")
    mod("unitree_sdk2py.utils.crc", CRC=_CRC)

    monkeypatch.syspath_prepend(str(REAL))
    monkeypatch.delitem(sys.modules, "g1_arm", raising=False)
    monkeypatch.setenv("G1_CONTROL_HZ", "50")   # keep the fake publish loop cheap
    return importlib.import_module("g1_arm")


def test_construction_survives_the_subscribe_thread_taking_the_sample(g1_arm):
    """One sample on the wire. The thread reads it; the constructor must not
    then ask the queue again and find it empty."""
    arm = g1_arm.G1_29_ArmController(motion_mode=True)
    try:
        assert arm.get_mode_machine() == _Subscriber.MODE_MACHINE
        # ...and it is what went onto the outgoing LowCmd, which is the only
        # reason the constructor asks for it at all.
        assert arm.msg.mode_machine == _Subscriber.MODE_MACHINE
    finally:
        arm.shutdown(ramp_s=0.0)


def test_asking_before_any_lowstate_refuses_instead_of_guessing(g1_arm):
    arm = g1_arm.G1_29_ArmController.__new__(g1_arm.G1_29_ArmController)
    arm.mode_machine = None
    with pytest.raises(RuntimeError):
        arm.get_mode_machine()


def test_the_getter_does_not_touch_the_subscriber_queue():
    """Guard the patch itself, in the style the rest of this suite uses."""
    body = (REAL / "g1_arm.py").read_text()
    i = body.index("class G1_29_ArmController")
    j = body.index("class G1_23_ArmController")
    getter = body[i:j].split("def get_mode_machine")[1].split("\n    def ")[0]
    code = re.sub(r"\'\'\'.*?\'\'\'", "", getter, flags=re.S)   # the docstring explains the old bug by name
    assert "lowstate_subscriber.Read()" not in code, \
        "get_mode_machine races the subscribe thread for the queue again"


def test_mode_machine_is_set_before_the_buffer_the_constructor_waits_on():
    """Ordering, not just presence -- the narrow half of the same race.

    The constructor's gate is `while not self.lowstate_buffer.GetData()`. If
    the subscribe thread publishes the buffer BEFORE recording mode_machine,
    that gate can open one statement early and the constructor's
    get_mode_machine() raises "G1 low state is not ready." instead of
    returning. Writing mode_machine first makes "the buffer has data" imply
    "mode_machine is set", so the gate cannot open too soon.
    """
    body = (REAL / "g1_arm.py").read_text()
    i = body.index("class G1_29_ArmController")
    j = body.index("class G1_23_ArmController")
    thread = body[i:j].split("def _subscribe_motor_state")[1].split("\n    def ")[0]
    assert thread.index("self.mode_machine = msg.mode_machine") \
        < thread.index("self.lowstate_buffer.SetData("), \
        "the subscribe thread opens the constructor's gate before mode_machine exists"
