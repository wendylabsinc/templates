import unittest
import time
from unittest.mock import AsyncMock

from g1_controller import G1Controller, _choose_network_interface


class ChooseNetworkInterfaceTests(unittest.TestCase):
    def test_uses_explicit_interface(self):
        addresses = {"eth0": "172.17.0.2", "enP8p1s0": "192.168.123.164"}

        self.assertEqual(_choose_network_interface("eth0", addresses), "eth0")

    def test_auto_selects_robot_bus_interface(self):
        addresses = {"eth0": "172.17.0.2", "enP8p1s0": "192.168.123.164"}

        self.assertEqual(
            _choose_network_interface("auto", addresses),
            "enP8p1s0",
        )

    def test_auto_fails_with_actionable_message(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "NETWORK_INTERFACE.*192\\.168\\.123\\.0/24",
        ):
            _choose_network_interface("auto", {"eth0": "172.17.0.2"})


class VelocitySafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_fsm_lookup_failure_never_calls_move(self):
        controller = G1Controller()
        controller._loco_client = object()
        controller._get_fsm_sync = lambda: (_ for _ in ()).throw(
            RuntimeError("FSM unavailable")
        )
        controller._call_sdk = AsyncMock()

        result = await controller.set_velocity(vx=0.2)

        self.assertIn("ignored", result)
        self.assertIn("FSM unavailable", result)
        controller._call_sdk.assert_not_awaited()

    async def test_fresh_running_readback_allows_move(self):
        controller = G1Controller()
        controller._loco_client = object()
        controller._latest_fsm_id = 801
        controller._latest_fsm_at = time.monotonic()
        controller._latest_fsm_error = None
        controller._call_sdk = AsyncMock()
        controller._arm_watchdog = AsyncMock()

        result = await controller.set_velocity(vx=0.2)

        self.assertIn("velocity vx=0.20", result)
        controller._call_sdk.assert_awaited_once_with("Move", 0.2, 0.0, 0.0)

    async def test_stale_running_readback_never_calls_move(self):
        controller = G1Controller()
        controller._loco_client = object()
        controller._latest_fsm_id = 801
        controller._latest_fsm_at = time.monotonic() - 10.0
        controller._call_sdk = AsyncMock()

        result = await controller.set_velocity(vx=0.2)

        self.assertIn("stale", result)
        controller._call_sdk.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
