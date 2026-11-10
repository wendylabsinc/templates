"""The right hand's fingers were crossed, and the hands were open loop.

DEX3_RIGHT is literally DEX3_LEFT with "left" replaced, so the bridge speaks
one order for both hands:

    thumb_0, thumb_1, thumb_2, middle_0, middle_1, index_0, index_1

Dex3_1_Left_JointIndex agrees. Dex3_1_Right_JointIndex puts INDEX before
MIDDLE. ctrl_dual_hand copied slot for slot, so on the right hand every
middle-finger command drove the index finger and vice versa.

It could not be seen with a trigger grip -- grip_q moves all seven together
and swapping two changes nothing visible -- which is why it survived to here.
"""
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "teleop" / "bridge"))
import retarget_g1 as rg                                        # noqa: E402

BRIDGE_ORDER = ["thumb_0", "thumb_1", "thumb_2",
                "middle_0", "middle_1", "index_0", "index_1"]


@pytest.fixture
def dex3(monkeypatch):
    """Import real/g1_dex3.py against a fake unitree_sdk2py."""
    class _Pub:
        def __init__(self, *a): self.msgs = []
        def Init(self): pass
        def Write(self, m): self.msgs.append(m)

    class _Sub:
        latest = None
        def __init__(self, *a): pass
        def Init(self): pass
        def Read(self): return _Sub.latest

    def _cmd():
        return types.SimpleNamespace(
            motor_cmd=[types.SimpleNamespace(mode=0, q=0.0, dq=0.0, tau=0.0,
                                             kp=0.0, kd=0.0) for _ in range(7)])

    def mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, name, m)

    mod("unitree_sdk2py")
    mod("unitree_sdk2py.core")
    mod("unitree_sdk2py.core.channel", ChannelPublisher=_Pub, ChannelSubscriber=_Sub)
    mod("unitree_sdk2py.idl")
    mod("unitree_sdk2py.idl.unitree_hg")
    mod("unitree_sdk2py.idl.unitree_hg.msg")
    mod("unitree_sdk2py.idl.unitree_hg.msg.dds_", HandCmd_=object, HandState_=object)
    mod("unitree_sdk2py.idl.default", unitree_hg_msg_dds__HandCmd_=_cmd)
    # A plain insert, NOT monkeypatch.syspath_prepend: that snapshots and
    # restores sys.path wholesale, and undoing it here dropped the bridge
    # entry for every test module that runs after this one alphabetically.
    real = str(ROOT / "onboard")
    if real not in sys.path:
        sys.path.insert(0, real)
    monkeypatch.delitem(sys.modules, "g1_dex3", raising=False)
    import importlib
    m = importlib.import_module("g1_dex3")
    m._FakeSub = _Sub
    return m


def test_the_bridge_speaks_one_order_for_both_hands():
    """The premise. If this ever stops being true the mapping below is wrong."""
    assert [n.replace("left_hand_", "").replace("_joint", "") for n in rg.DEX3_LEFT] == BRIDGE_ORDER
    assert [n.replace("right_hand_", "").replace("_joint", "") for n in rg.DEX3_RIGHT] == BRIDGE_ORDER


def test_the_right_hand_enum_really_does_swap_index_and_middle(dex3):
    """Not a typo on our side: the hardware orders differ between hands."""
    L = [j.name for j in dex3.Dex3_1_Left_JointIndex]
    R = [j.name for j in dex3.Dex3_1_Right_JointIndex]
    assert L.index("kLeftHandMiddle0") < L.index("kLeftHandIndex0")
    assert R.index("kRightHandIndex0") < R.index("kRightHandMiddle0")


def test_a_middle_finger_command_reaches_the_middle_finger_on_both_hands(dex3):
    """The bug, stated as the property it broke.

    Sends 1.0 for middle_0 alone and checks which hardware slot moved. Before
    the fix the right hand put it on kRightHandIndex0.
    """
    s = dex3.Dex3Sender()
    q = [0.0] * 7
    q[BRIDGE_ORDER.index("middle_0")] = 1.0
    s.ctrl_dual_hand(q, q)
    lslot = int(dex3.Dex3_1_Left_JointIndex.kLeftHandMiddle0)
    rslot = int(dex3.Dex3_1_Right_JointIndex.kRightHandMiddle0)
    assert s._left.motor_cmd[lslot].q == 1.0
    assert s._right.motor_cmd[rslot].q == 1.0, "right middle command went elsewhere"
    assert s._right.motor_cmd[int(dex3.Dex3_1_Right_JointIndex.kRightHandIndex0)].q == 0.0


def test_an_index_command_reaches_the_index_finger(dex3):
    s = dex3.Dex3Sender()
    q = [0.0] * 7
    q[BRIDGE_ORDER.index("index_1")] = 0.7
    s.ctrl_dual_hand(q, q)
    assert s._right.motor_cmd[int(dex3.Dex3_1_Right_JointIndex.kRightHandIndex1)].q == 0.7
    assert s._right.motor_cmd[int(dex3.Dex3_1_Right_JointIndex.kRightHandMiddle1)].q == 0.0


def test_every_bridge_slot_maps_somewhere_exactly_once(dex3):
    for order in (dex3._BRIDGE_TO_HW_LEFT, dex3._BRIDGE_TO_HW_RIGHT):
        assert sorted(int(x) for x in order) == list(range(7))


def test_measured_returns_the_bridge_order_not_the_hardware_order(dex3):
    """Feedback has to come back in the order the rest of the code speaks,
    or reading it re-introduces the same crossing it just fixed."""
    s = dex3.Dex3Sender()
    hw = [0.0] * 7
    hw[int(dex3.Dex3_1_Right_JointIndex.kRightHandMiddle0)] = 0.9
    dex3._FakeSub.latest = types.SimpleNamespace(
        motor_state=[types.SimpleNamespace(q=v) for v in hw])
    got = s.measured()["right"]
    assert got[BRIDGE_ORDER.index("middle_0")] == 0.9
    assert got[BRIDGE_ORDER.index("index_0")] == 0.0


def test_a_hand_that_will_not_report_is_not_fatal(dex3):
    """The hands gripped for weeks without this; losing it must cost nothing."""
    s = dex3.Dex3Sender()
    s._lsub = s._rsub = None
    assert s.measured() == {"left": None, "right": None}


def test_the_state_read_is_rate_limited_off_the_frame_path(dex3, monkeypatch):
    """measured() is called from ack_message, which runs on EVERY frame.

    Reading two DDS topics at 90 Hz put blocking reads back on the hot path
    and cost it measurably: arm_cmd_ms went 0.27 -> 6.8 ms and the liveness
    watchdog started firing -- the same starvation the publisher loop had just
    been fixed for. Grasp detection needs about 10 Hz.
    """
    s = dex3.Dex3Sender()
    reads = {"n": 0}

    class _Counting:
        def Read(self_inner):
            reads["n"] += 1
            return types.SimpleNamespace(
                motor_state=[types.SimpleNamespace(q=0.0) for _ in range(7)])

    s._lsub = s._rsub = _Counting()
    for _ in range(200):                       # ~2 s of frames at 90 Hz
        s.measured()
    assert reads["n"] <= 4, f"{reads['n']} DDS reads for 200 frames"


def test_between_reads_it_returns_the_last_sample(dex3):
    """A 10 Hz sensor holds its value between samples; that is not staleness."""
    s = dex3.Dex3Sender()
    hw = [0.0] * 7
    hw[int(dex3.Dex3_1_Right_JointIndex.kRightHandMiddle1)] = 1.23
    dex3._FakeSub.latest = types.SimpleNamespace(
        motor_state=[types.SimpleNamespace(q=v) for v in hw])
    first = s.measured()["right"]
    dex3._FakeSub.latest = None                # nothing new on the wire
    again = s.measured()["right"]
    assert again == first


# ---- stiffness ---------------------------------------------------------------

def test_a_command_after_relax_restores_the_stiffness(dex3):
    """The bug the operator hit as "when i press home and then arm the hands
    are not responding".

    relax() zeroes kp/kd, and hold() calls relax() -- so every hold, every
    go_home (which holds first) and every watchdog freeze left the fingers
    limp. ctrl_dual_hand only wrote `q`, so nothing ever put it back: the
    hands stopped responding for the rest of the session however many times
    the operator re-armed. Measured on the robot: commanded 0.000 (open),
    measured -0.921 (shut), not moving.
    """
    s = dex3.Dex3Sender(kp=3.0, kd=0.2)
    s.relax()
    assert all(s._right.motor_cmd[j].kp == 0.0 for j in range(7))
    s.ctrl_dual_hand([0.0] * 7, [0.0] * 7)
    assert all(s._right.motor_cmd[j].kp == 3.0 for j in range(7)), \
        "a hand commanded after a relax is still limp"
    assert all(s._left.motor_cmd[j].kd == 0.2 for j in range(7))


def test_the_gains_are_written_every_frame_not_only_at_construction(dex3):
    """So the message says what it means, rather than depending on nobody
    having called relax() since startup."""
    s = dex3.Dex3Sender(kp=3.0, kd=0.2)
    for j in range(7):                      # something else zeroed them
        s._right.motor_cmd[j].kp = 0.0
    s.ctrl_dual_hand([0.0] * 7, [0.5] * 7)
    assert all(s._right.motor_cmd[j].kp == 3.0 for j in range(7))


def test_a_command_reenables_every_motor_after_a_latched_disable(dex3):
    """A later command must restore the per-motor enable bits as well as gains.

    A Dex3 knuckle can stop following after resistance while its outer joint
    keeps moving.  The command's mode used to be initialized once and then
    never refreshed, so a motor that latched disabled could not recover until
    the sender was rebuilt.
    """
    s = dex3.Dex3Sender(kp=3.0, kd=0.2)
    for msg in (s._left, s._right):
        for j in range(7):
            msg.motor_cmd[j].mode = 0
    s.ctrl_dual_hand([0.0] * 7, [0.5] * 7)
    for msg in (s._left, s._right):
        assert [cmd.mode for cmd in msg.motor_cmd] == [
            dex3._RIS_Mode(id=j, status=0x01).to_uint8() for j in range(7)
        ]


def test_relax_still_relaxes(dex3):
    """It is the e-stop path: hold must still stop the hand squeezing someone."""
    s = dex3.Dex3Sender(kp=3.0, kd=0.2)
    s.ctrl_dual_hand([0.0] * 7, [0.0] * 7)
    s.relax()
    assert all(s._left.motor_cmd[j].kp == 0.0 for j in range(7))
    assert all(s._right.motor_cmd[j].kd == 0.0 for j in range(7))


def test_the_closed_pose_matches_grip_q(dex3):
    """CLOSED_LEFT/RIGHT are grip_q(side, 1.0), which cannot be imported on the
    robot -- the bridge does not run there. So they are duplicated, and a
    duplicate that is not checked is a duplicate that drifts."""
    for side, const in (("left", dex3.CLOSED_LEFT), ("right", dex3.CLOSED_RIGHT)):
        want = rg.grip_q(side, 1.0)
        assert len(const) == 7
        for i, (a, b) in enumerate(zip(const, want)):
            assert abs(a - b) < 1e-6, f"{side} {BRIDGE_ORDER[i]}: {a} vs grip_q {b}"


def test_going_home_closes_the_hands(dex3):
    """The operator asked for it: home used to open them, on the reasoning
    that a fist is not a known starting pose. A closed hand is equally known,
    and it is the compact shape to move an arm across a table in."""
    src = (ROOT / "onboard" / "g1_onboard.py").read_text()
    body = src.split("    def go_home(")[1].split("\n    def ")[0]
    assert "CLOSED_LEFT" in body and "CLOSED_RIGHT" in body
    assert "[0.0] * HAND_DIM" not in body, "that is the OPEN pose"
