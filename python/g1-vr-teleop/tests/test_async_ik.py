"""The arm IK, off the frame loop.

Measured inline, one solve costs ~95 ms and set the rate for the whole chain:
85 fps of hand data in, 10.6 frames/s out. The fingers need no IK and were
paying for it anyway.

These tests use a deliberately slow fake solver. A fast fake would let a broken
implementation pass by accident -- the whole point is what happens while a
solve is still running.
"""
import os
import sys
import threading
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teleop", "bridge"))
import retarget_g1 as rg  # noqa: E402


class SlowIK:
    """A solver that takes its time, and says what it was asked."""

    def __init__(self, ms=60, fail=False, raises=False):
        self.ms = ms
        self.fail = fail
        self.raises = raises
        self.calls = 0
        self.seen = []
        self.lock = threading.Lock()

    def solve_ik(self, Tl, Tr, seed):
        with self.lock:
            self.calls += 1
            self.seen.append(Tl)
        time.sleep(self.ms / 1000.0)
        if self.raises:
            raise RuntimeError("IPOPT gave up")
        if self.fail:
            return np.full(14, np.nan), False
        # Encode the request so a test can tell WHICH target a result came from.
        return np.full(14, float(Tl)), True


def _worker(ik, q0=None):
    w = rg.ArmIKWorker(ik, np.zeros(14) if q0 is None else q0)
    return w


def _wait(cond, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.005)
    return False


def test_submitting_does_not_block_the_caller():
    """The property the whole change exists for."""
    ik = SlowIK(ms=120)
    w = _worker(ik)
    t0 = time.perf_counter()
    for _ in range(20):
        w.submit(1.0, 1.0)
    dt = (time.perf_counter() - t0) * 1e3
    assert dt < 50, f"20 submits took {dt:.0f} ms; the caller is waiting on the solve"
    w.stop()


def test_only_the_newest_target_is_solved():
    """A queue would answer questions the operator has already moved past."""
    ik = SlowIK(ms=80)
    w = _worker(ik)
    w.submit(1.0, 1.0)                       # this one starts solving
    assert _wait(lambda: ik.calls == 1)
    for i in range(2, 12):                   # ten more arrive during the solve
        w.submit(float(i), float(i))
    assert _wait(lambda: ik.calls >= 2, 3.0)
    time.sleep(0.3)
    assert ik.calls <= 3, f"{ik.calls} solves for 11 targets; a backlog is building"
    assert ik.seen[-1] == 11.0, f"solved {ik.seen[-1]}, not the newest target"
    assert w.n_dropped >= 8, "superseded targets were not counted as dropped"
    w.stop()


def test_the_latest_solution_is_available_while_a_solve_runs():
    ik = SlowIK(ms=80)
    w = _worker(ik)
    w.submit(7.0, 7.0)
    assert _wait(lambda: w.n_solved == 1)
    first, age = w.latest()
    assert first[0] == pytest.approx(7.0)
    assert age is not None and age < 1.0
    w.submit(9.0, 9.0)                       # second solve now in flight
    mid, _ = w.latest()
    assert mid[0] == pytest.approx(7.0), "a half-finished solve was handed out"
    assert _wait(lambda: w.n_solved == 2)
    assert w.latest()[0][0] == pytest.approx(9.0)
    w.stop()


def test_a_failed_solve_keeps_the_previous_pose():
    """An arm handed a NaN is commanded somewhere nobody asked for."""
    ik = SlowIK(ms=10)
    w = _worker(ik)
    w.submit(3.0, 3.0)
    assert _wait(lambda: w.n_solved == 1)
    ik.fail = True
    w.submit(4.0, 4.0)
    assert _wait(lambda: w.n_failed == 1)
    q, _ = w.latest()
    assert np.all(np.isfinite(q)), "a non-finite solution escaped"
    assert q[0] == pytest.approx(3.0), "the failed solve overwrote a good pose"
    w.stop()


def test_a_raising_solver_does_not_kill_the_worker():
    ik = SlowIK(ms=10, raises=True)
    w = _worker(ik, np.full(14, 5.0))
    w.submit(1.0, 1.0)
    assert _wait(lambda: w.n_failed >= 1)
    ik.raises = False
    w.submit(2.0, 2.0)
    assert _wait(lambda: w.n_solved >= 1), "the worker died on an exception"
    assert w.latest()[0][0] == pytest.approx(2.0)
    w.stop()


def test_the_seed_is_the_last_solution_not_a_constant():
    """Warm-starting is most of why IPOPT is only 95 ms and not much worse."""
    seeds = []

    class SeedSpy(SlowIK):
        def solve_ik(self, Tl, Tr, seed):
            seeds.append(np.asarray(seed).copy())
            return super().solve_ik(Tl, Tr, seed)

    ik = SeedSpy(ms=5)
    w = _worker(ik)
    w.submit(2.0, 2.0)
    assert _wait(lambda: w.n_solved == 1)
    w.submit(6.0, 6.0)
    assert _wait(lambda: w.n_solved == 2)
    assert seeds[0][0] == pytest.approx(0.0)
    assert seeds[1][0] == pytest.approx(2.0), "the second solve did not warm-start"
    w.stop()


def test_stats_report_what_happened():
    ik = SlowIK(ms=5)
    w = _worker(ik)
    w.submit(1.0, 1.0)
    assert _wait(lambda: w.n_solved == 1)
    st = w.stats()
    assert st["solved"] == 1 and st["failed"] == 0
    assert st["last_ms"] >= 0 and st["age_s"] is not None
    w.stop()


def test_an_unstarted_worker_reports_no_age():
    w = _worker(SlowIK(ms=5))
    q, age = w.latest()
    assert age is None, "age must be None before the first solve, not 0"
    assert np.all(q == 0.0)
    w.stop()
