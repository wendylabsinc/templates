import unittest

from g1_controller import _choose_network_interface


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


if __name__ == "__main__":
    unittest.main()
