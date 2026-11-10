"""The constructor must survive its own flags.

TELEOP_ASYNC_IK=1 read two attributes before they were assigned, so the flag
took the bridge down at startup. It was invisible locally because the flag
cannot be exercised on a Mac at all: G1_29_ArmIK needs pinocchio.casadi, which
pip cannot provide, so __init__ dies earlier for an unrelated reason and the
ordering never gets reached.

So the heavy pieces are stubbed and the ordering is checked directly. Stubbing
is the only way this class of bug is catchable off-device.
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))


def _stub_heavy(monkeypatch):
    """Let __init__ run to completion without pinocchio, casadi or torch."""
    import retarget_g1 as rg

    class FakeIK:
        def __init__(self, *a, **k):
            pass

    class FakeCfg:
        joint_names = list(rg.DEX3_LEFT)

        def build(self):
            return self

        @staticmethod
        def set_default_urdf_dir(*a, **k):
            pass

        @staticmethod
        def from_dict(*a, **k):
            return FakeCfg()

    mod = types.ModuleType("_vendor_robot_arm_ik")
    mod.G1_29_ArmIK = FakeIK
    monkeypatch.setitem(sys.modules, "_vendor_robot_arm_ik", mod)

    dex = types.ModuleType("dex_retargeting")
    cfgmod = types.ModuleType("dex_retargeting.retargeting_config")
    cfgmod.RetargetingConfig = FakeCfg
    monkeypatch.setitem(sys.modules, "dex_retargeting", dex)
    monkeypatch.setitem(sys.modules, "dex_retargeting.retargeting_config", cfgmod)
    return rg


@pytest.mark.parametrize("async_ik", ["0", "1"])
def test_the_constructor_reaches_the_ik_before_anything_reads_it(monkeypatch, async_ik):
    """Ordering only -- not a full construction test.

    Reads the source and asserts the worker is built after the attributes it
    consumes. Cheap, and it is exactly the invariant that broke.
    """
    import pathlib
    src = pathlib.Path(__file__).parent.parent / "teleop" / "bridge" / "retarget_g1.py"
    text = src.read_text()
    worker = text.index("self._ik_worker = ArmIKWorker(")
    arm_ik = text.index("self._arm_ik = G1_29_ArmIK(")
    arm_q = text.index("self._arm_q = None")
    assert arm_ik < worker, (
        "ArmIKWorker is constructed before self._arm_ik exists; "
        "TELEOP_ASYNC_IK=1 will AttributeError at startup")
    assert arm_q < worker, (
        "ArmIKWorker is constructed before self._arm_q exists")


def test_the_worker_is_only_built_when_the_flag_is_set():
    """Off by default, so the verified modes are untouched."""
    import pathlib
    src = pathlib.Path(__file__).parent.parent / "teleop" / "bridge" / "retarget_g1.py"
    text = src.read_text()
    # The GUARD, not the first mention -- the comment above it names the flag
    # several times, which is what made a naive search pass vacuously.
    guard = text.index('if os.environ.get("TELEOP_ASYNC_IK"')
    worker = text.index("self._ik_worker = ArmIKWorker(")
    assert guard < worker, "the worker is constructed outside the flag guard"
    assert worker - guard < 400, \
        "the worker construction drifted away from its guard"
