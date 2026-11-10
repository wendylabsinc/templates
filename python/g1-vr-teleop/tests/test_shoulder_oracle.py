"""Independent second opinions on the shoulder, from libraries we did not write.

Shoulder mode is exact on paper and has never run on hardware. Its checks today
are a closed form, verified against a forward kinematics helper in
`test_shoulder.py` -- both derived from the same URDF, by the same hand, in the
same sitting. That is re-doing your own arithmetic: it catches slips and not
misunderstandings, and `HANDOFF.md` records the misunderstanding version
happening twice ("summing joint offsets while ignoring their rpy") plus once
where the test was built on the same mistake as the code and agreed with it.

This file adds two oracles -- sources of truth built by other people:

  * **Pinocchio** parses the URDF and does the forward kinematics. It cannot
    make the rpy mistake, because it is not our code.
  * **Pink** solves the inverse problem by a completely different method
    (weighted-task differential IK against a QP) from our hand-derived closed
    form.

Neither needs `pinocchio.casadi`, which is why these run on a laptop while the
vendored IPOPT path does not. That is the point: the check lives in the fast
suite instead of in the container. They SKIP if the two packages are absent,
so nothing here is required to run the rest of the suite:

    .venv312/bin/pip install pin-pink qpsolvers quadprog

WHAT THESE CANNOT DO. Both oracles read the same URDF we do. If the URDF
disagrees with the physical robot -- the leading suspect for the operator's
report that shoulder pitch is inverted -- both will be wrong together and agree
with each other. These test the SOLVE, not the MODEL. Hardware acceptance is
still the operator reaching and saying where the robot went.
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pin = pytest.importorskip("pinocchio", reason="oracle needs pinocchio")
pink = pytest.importorskip("pink", reason="oracle needs pin-pink")
pytest.importorskip("qpsolvers", reason="oracle needs qpsolvers")

from pink import Configuration, solve_ik  # noqa: E402
from pink.tasks import FrameTask, PostureTask  # noqa: E402

import retarget_g1 as rg  # noqa: E402
from test_shoulder import wrist_position  # noqa: E402

_URDF_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..",
                                         "teleop", "assets", "g1"))
SIDES = ("left", "right")
# The joints the shoulder path drives, plus the two it deliberately holds.
_FREE = ("shoulder_pitch_joint", "shoulder_roll_joint",
         "shoulder_yaw_joint", "elbow_joint")


class ArmModel:
    """One arm, reduced to the four joints above, with Pinocchio's own FK."""

    def __init__(self, side: str):
        self.side = side
        # Kinematics only. `RobotWrapper.BuildFromURDF` also loads every
        # collision and visual mesh, which costs 4.4 s per arm and is of no use
        # to a joint-angle comparison.
        m = pin.buildModelFromUrdf(
            os.path.join(_URDF_DIR, "g1_body29_hand14.urdf"))
        keep = {f"{side}_{j}" for j in _FREE}
        lock = [m.names[i] for i in range(1, m.njoints) if m.names[i] not in keep]
        self.model = pin.buildReducedModel(
            m, [m.getJointId(n) for n in lock], np.zeros(m.nq))
        self.data = self.model.createData()
        self.order = [self.model.names[i] for i in range(1, self.model.njoints)]
        self.f_wrist = self.model.getFrameId(f"{side}_wrist_roll_joint")
        self.f_shoulder = self.model.getFrameId(f"{side}_shoulder_roll_joint")

    def q(self, pitch, roll, elbow, yaw=0.0):
        v = {f"{self.side}_shoulder_pitch_joint": pitch,
             f"{self.side}_shoulder_roll_joint": roll,
             f"{self.side}_shoulder_yaw_joint": yaw,
             f"{self.side}_elbow_joint": elbow}
        return np.array([v[n] for n in self.order], float)

    def _place(self, q):
        pin.forwardKinematics(self.model, self.data, np.asarray(q, float))
        pin.updateFramePlacements(self.model, self.data)

    def wrist_from_shoulder(self, pitch, roll, elbow, yaw=0.0):
        """Shoulder-roll origin -> wrist, the same quantity `wrist_position` returns."""
        self._place(self.q(pitch, roll, elbow, yaw))
        return (self.data.oMf[self.f_wrist].translation
                - self.data.oMf[self.f_shoulder].translation)

    def wrist_target(self, pitch, roll, elbow, yaw=0.0):
        self._place(self.q(pitch, roll, elbow, yaw))
        return pin.SE3(self.data.oMf[self.f_wrist])

    def solve(self, target, elbow, yaw=0.0, max_iters=300, dt=0.02, tol=1e-7):
        """Pink's answer for pitch/roll, with yaw and the elbow pinned.

        Pinned by a heavy posture cost rather than by locking, so the solver
        still sees the same model our closed form reasons about.

        Stops when the velocity it wants is negligible rather than after a fixed
        count: converged is converged, and a fixed 300 spent most of the test
        suite's wall clock re-confirming an answer it already had.
        """
        cfg = Configuration(self.model, self.data, self.q(0.0, 0.0, elbow, yaw))
        frame = FrameTask(f"{self.side}_wrist_roll_joint",
                          position_cost=1.0, orientation_cost=0.0)
        frame.set_target(target)
        posture = PostureTask(cost=np.array(
            [1e-4 if ("shoulder_pitch" in n or "shoulder_roll" in n) else 10.0
             for n in self.order]))
        posture.set_target(self.q(0.0, 0.0, elbow, yaw))
        for _ in range(max_iters):
            v = solve_ik(cfg, [frame, posture], dt, solver="quadprog")
            cfg.integrate_inplace(v, dt)
            if float(np.max(np.abs(v))) * dt < tol:
                break
        got = dict(zip(self.order, cfg.q))
        return (got[f"{self.side}_shoulder_pitch_joint"],
                got[f"{self.side}_shoulder_roll_joint"])


@pytest.fixture(scope="module")
def arms():
    return {s: ArmModel(s) for s in SIDES}


POSES = [(p, r, e)
         for p in (-0.8, -0.3, 0.0, 0.4)
         for r in (-0.4, 0.0, 0.6)
         for e in (0.2, 0.8, 1.2)]



def _operator_wrist(side, pitch, roll, elbow):
    """An operator wrist position that asks for exactly this (pitch, roll, elbow).

    The distance is not free: `_shoulder_q` derives the elbow from it and then
    solves the shoulder against `shoulder_to_wrist` AT THAT ELBOW. Picking a
    distance and an elbow independently asks the closed form one question and
    grades it against the answer to another -- which is what an earlier version
    of this test did, and it reported an 11 mm error that was entirely its own.
    """
    lo, hi = rg.G1Retargeter._ELBOW_SPAN
    near, far = rg.G1Retargeter._ELBOW_CAL[side]
    dist = near + (elbow - lo) / (hi - lo) * (far - near)
    w = rg.shoulder_to_wrist(side, elbow)
    d = rg._ry(pitch) @ rg._rx(roll) @ (w / np.linalg.norm(w))
    pre = rg.G1Retargeter._SHOULDER_PRE[side]
    return (rg.G1Retargeter._SHOULDER[side]
            + dist * (rg._R_XR_TO_ROBOT.T @ (pre @ d)))


# --------------------------------------------------------------------------- #
# Oracle 1 -- Pinocchio checks our forward kinematics
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("side", SIDES)
def test_our_forward_kinematics_matches_pinocchio(arms, side):
    """`wrist_position` is hand-rolled from URDF offsets and rpy matrices.

    Two of the three bugs in HANDOFF.md came from summing joint offsets while
    ignoring their rpy. Pinocchio parses the same URDF and cannot make that
    mistake, so agreement here retires the failure mode -- for the helper that
    every other shoulder test is graded against.
    """
    worst = max(
        float(np.linalg.norm(wrist_position(side, p, r, e)
                             - arms[side].wrist_from_shoulder(p, r, e)))
        for p, r, e in POSES)
    assert worst < 1e-6, f"{side}: FK differs by {worst * 1000:.4f} mm"


# --------------------------------------------------------------------------- #
# Oracle 2 -- Pink checks our inverse kinematics
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("side", SIDES)
def test_pink_recovers_the_pose_that_generated_the_target(arms, side):
    """Sanity on the oracle itself before it is used to judge anything.

    A target is produced by forward kinematics from known angles, so the right
    answer is known by construction. If Pink cannot recover it, Pink is not fit
    to grade the closed form.
    """
    a = arms[side]
    for p, r, e in POSES[::5]:
        gp, gr = a.solve(a.wrist_target(p, r, e), e)
        assert abs(gp - p) < 1e-3 and abs(gr - r) < 1e-3, (
            f"{side}: pink got ({gp:+.4f}, {gr:+.4f}) for ({p:+.2f}, {r:+.2f})")


_PINK_CACHE: dict = {}


def _pink(a, p, r, elbow):
    """Pink's pitch/roll for this pose, memoised.

    Both comparisons below ask for the same solves; the QP is the slowest thing
    in the file and there is no reason to run it twice."""
    key = (a.side, p, r, round(elbow, 9))
    if key not in _PINK_CACHE:
        _PINK_CACHE[key] = a.solve(a.wrist_target(p, r, elbow), elbow)
    return _PINK_CACHE[key]


def _solver():
    """A retargeter with no continuity history.

    `_shoulder_q` runs `_degap`, which rejects a step larger than 10 deg because
    a real hand cannot teleport. These tests sweep a grid and DO teleport, so a
    shared instance answers the second pose with the first pose's angles. That
    is the gate working as designed; give every pose a clean one.
    """
    return rg.G1Retargeter.__new__(rg.G1Retargeter)


@pytest.mark.parametrize("side", SIDES)
def test_the_closed_form_agrees_with_pink(arms, side):
    """The comparison this file exists for.

    Same target to both. Ours is a hand-derived inversion; Pink is a QP over
    task jacobians. They share no code and no author, so agreement is evidence
    neither is wrong in a way the other is not.
    """
    a = arms[side]
    worst, worst_at = 0.0, None
    for p, r, e in POSES:
        want = _operator_wrist(side, p, r, e)
        q = _solver()._shoulder_q(side, {"wrist": want.tolist()})
        if not q:
            continue                       # unreachable at this elbow; not our case
        elbow = float(_solver()._elbow_q(side, {"wrist": want.tolist()}))
        tp, tr = _pink(a, p, r, elbow)
        d = max(abs(q[f"{side}_shoulder_pitch_joint"] - tp),
                abs(q[f"{side}_shoulder_roll_joint"] - tr))
        if d > worst:
            worst, worst_at = d, (p, r, e)
    assert np.degrees(worst) < 1.0, (
        f"{side}: closed form and pink differ by {np.degrees(worst):.2f} deg "
        f"at pitch/roll/elbow {worst_at}")


@pytest.mark.parametrize("side", SIDES)
def test_pink_cannot_place_the_wrist_better_than_we_do(arms, side):
    """The weaker but more robust form of the same question.

    Joint angles can differ legitimately near a singularity while both answers
    put the hand in the same place. What must not happen is Pink placing the
    wrist materially closer to the target than the closed form does -- that
    would mean we are leaving reachable accuracy behind.
    """
    a = arms[side]
    for p, r, e in POSES:
        want = _operator_wrist(side, p, r, e)
        q = _solver()._shoulder_q(side, {"wrist": want.tolist()})
        if not q:
            continue
        elbow = float(_solver()._elbow_q(side, {"wrist": want.tolist()}))
        goal = a.wrist_from_shoulder(p, r, elbow)
        ours = a.wrist_from_shoulder(q[f"{side}_shoulder_pitch_joint"],
                                     q[f"{side}_shoulder_roll_joint"], elbow)
        tp, tr = _pink(a, p, r, elbow)
        pink_pos = a.wrist_from_shoulder(tp, tr, elbow)

        ours_err = float(np.linalg.norm(ours - goal))
        pink_err = float(np.linalg.norm(pink_pos - goal))
        assert ours_err <= pink_err + 1e-3, (
            f"{side} at ({p:+.2f},{r:+.2f},{e:.2f}): pink placed the wrist "
            f"{pink_err * 1000:.2f} mm from the target, we placed it "
            f"{ours_err * 1000:.2f} mm")
