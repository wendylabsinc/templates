"""The sink must stop sending when the hand data goes stale.

This is what lets the onboard dead-man work. The onboard watches for frames
STOPPING; the sink used to re-send its last pose at 100 Hz forever, so frames
never stopped and the dead-man could never fire. The two halves were each
defensible alone and unsafe together.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "teleop" / "bridge"))


class StalenessTests(unittest.TestCase):
    def setUp(self):
        import sink_realg1
        self.mod = sink_realg1
        # build without starting its threads
        self.sink = sink_realg1.RealG1Sink.__new__(sink_realg1.RealG1Sink)
        import threading
        self.sink._latest = None
        self.sink._latest_t = 0.0
        self.sink._lock = threading.Lock()
        self.sink.n_sent = self.sink.n_retargeted = 0

    def test_no_frame_yet_is_not_sent(self):
        self.assertIsNone(self.sink.fresh_frame())

    def test_a_fresh_frame_is_sent(self):
        self.sink._latest = {"arm": [0.0] * 14}
        self.sink._latest_t = time.monotonic()
        self.assertIsNotNone(self.sink.fresh_frame())

    def test_a_stale_frame_is_NOT_sent(self):
        """The whole point: silence is what trips the robot's dead-man."""
        self.sink._latest = {"arm": [0.0] * 14}
        self.sink._latest_t = time.monotonic() - 1.0
        self.assertIsNone(self.sink.fresh_frame())

    def test_the_boundary_is_the_stale_threshold(self):
        self.sink._latest = {"arm": [0.0] * 14}
        self.sink._latest_t = time.monotonic() - (self.sink.STALE_S / 2)
        self.assertIsNotNone(self.sink.fresh_frame())
        self.sink._latest_t = time.monotonic() - (self.sink.STALE_S * 2)
        self.assertIsNone(self.sink.fresh_frame())


class ValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import sink_realg1, threading
        self.sink = sink_realg1.RealG1Sink.__new__(sink_realg1.RealG1Sink)
        self.sink._latest = None
        self.sink._latest_t = 0.0
        self.sink._lock = threading.Lock()
        self.sink.n_sent = self.sink.n_retargeted = 0

    def _joints(self, value=0.1):
        from retarget_g1 import ARM_LEFT, ARM_RIGHT, DEX3_LEFT, DEX3_RIGHT
        return {n: value for n in ARM_LEFT + ARM_RIGHT + DEX3_LEFT + DEX3_RIGHT}

    async def test_a_complete_frame_is_accepted(self):
        await self.sink.apply(self._joints())
        self.assertIsNotNone(self.sink._latest)

    async def test_a_frame_missing_a_joint_is_refused(self):
        """Defaulting a missing joint to 0.0 is not 'leave it alone'; zero is a
        specific pose, and substituting it silently is a sudden move."""
        from retarget_g1 import ARM_LEFT
        j = self._joints()
        del j[ARM_LEFT[0]]
        await self.sink.apply(j)
        self.assertIsNone(self.sink._latest)

    async def test_a_non_finite_target_is_refused(self):
        j = self._joints()
        from retarget_g1 import ARM_LEFT
        j[ARM_LEFT[0]] = float("nan")
        await self.sink.apply(j)
        self.assertIsNone(self.sink._latest)


if __name__ == "__main__":
    unittest.main()
